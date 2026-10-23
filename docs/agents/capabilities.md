# What an agent can do: web app vs REST vs MCP

Every row is a capability of the PostDelegate web app. **Before** is what REST and MCP exposed before the coverage work; **Now** is today. REST paths are under `/api/v1`. Scope is the key's permission set plus its account allowlist; reads and writes tied to an account honour the allowlist, and ids from another workspace answer `404`.

| Capability | Before (REST / MCP) | Now (REST / MCP) | Permission |
|---|---|---|---|
| Posts: create, read, update, schedule, cancel | yes / create, schedule, get, cancel (no update) | unchanged / adds `update_post` (same rules as `PATCH /posts/{id}`) | `create_posts`, `publish_directly` |
| Posts: list with filters and pagination | none / `list_posts` (status only) | `GET /posts` / `list_posts` with `status`, `social_account_id`, `date_from`, `date_to`, `tag`, `search`, `limit`, `cursor` | read |
| Posts: delete | none (documented as unsupported) | `DELETE /posts/{id}` / `delete_post`; publishing or published posts refused (`409`) | `create_posts` (+ `edit_others_posts`) |
| Posts: duplicate | none | `POST /posts/{id}/duplicate` / `duplicate_post` | `create_posts` |
| Posts: tags and category | none | `tags`, `category_id` on create and PATCH / on `create_draft`, `schedule_post` | `create_posts` |
| Threads (parts), recurrence | yes | unchanged | `create_posts` (+ `publish_directly`) |
| Media upload and library | yes / yes | unchanged | `upload_media` |
| Accounts (list, capabilities) | yes / yes | unchanged | read |
| Approvals: pending list | none | `GET /approvals` / `list_approvals` | `approve_posts` or `create_posts` |
| Approvals: approve, request changes, reject | none | `POST /approvals/{id}/approve`, `/request-changes`, `/reject` / `review_post` (`decision`); same approval service as the web app (two-stage client flow, notifications, webhooks) | `approve_posts` |
| Team members (read) | none | `GET /members` / `list_members` (role and `@mention` handle; no emails) | read |
| Comments: list, add | none | `GET`/`POST /approvals/{id}/comments` / `list_post_comments`, `add_post_comment`; clients see and write `external` only | `create_posts` or `approve_posts` |
| Queues: list, read, add post, remove post | none | `GET /queues`, `GET /queues/{id}`, `POST /queues/{id}/entries`, `DELETE /queues/{id}/entries/{entry}` / `list_queues`, `get_queue`, `add_post_to_queue`, `remove_from_queue` | read; `create_posts` + `publish_directly` |
| Posting slots: list, create, update, delete | none | `/slots` / `list_posting_slots`, `create_posting_slot`, `update_posting_slot`, `delete_posting_slot` | read; `manage_social_accounts` |
| Next free slot for an account | none | `GET /slots/next-free` / `find_next_free_slot` | read |
| Best times to post for an account | Posting-slots page heatmap; "Best times" chips in the composer's schedule picker | `GET /best-times` / `get_best_times` (learned from the account's own posts, or labelled industry defaults when it has too few) | read |
| Make the best times the posting slots | "Suggest slots" with preview and confirm | `POST /best-times/apply` / `apply_best_times` (`mode` replace or add, `preview`) | `manage_social_accounts` |
| Evergreen recycling: list, add, update or pause, remove | Evergreen library; "Add to evergreen" on sent posts | `/evergreen` / `list_evergreen_items`, `add_to_evergreen`, `pause_evergreen_item`, `remove_from_evergreen`; pre-approving repeats also needs `approve_posts` | `create_posts` |
| Calendar events | none | `GET`/`POST /events`, `DELETE /events/{id}` / `list_calendar_events`, `create_calendar_event`, `delete_calendar_event` | read; `create_posts` |
| Ideas (kanban) and columns | none | `/ideas`, `/idea-groups` / `list_ideas`, `get_idea`, `create_idea`, `update_idea`, `delete_idea`, `list_idea_groups`, `create_idea_group`, `delete_idea_group` | `create_posts` |
| Templates | none | `/templates` / `list_templates`, `get_template`, `create_template`, `update_template`, `delete_template`; a template aimed at an account outside the allowlist is hidden, like a post | read; `create_posts` |
| Tags | none | `/tags` / `list_tags`, `create_tag`, `rename_tag`, `delete_tag`; rename and delete also update posts and ideas, and are refused when the tag is on posts outside the allowlist | read; `create_posts` |
| Content categories | none | `/categories` / `list_categories`, `create_category`, `update_category`, `delete_category` | read; `create_posts` |
| Hashtag sets, shared signatures (read) | REST read / none | REST read / `list_hashtag_sets`, `list_signatures` | read |
| Autonomy policy and the agent audit trail | Settings > Autonomy; Activity page | `GET`/`PATCH /autonomy`, `GET /autonomy/actions` / `get_autonomy_policy`, `update_autonomy_policy`, `list_agent_actions`; a key can only tighten the policy | read policy: any key; update: `manage_workspace_settings`; actions: `create_posts` or `manage_workspace_settings` |
| Brand voice | none | `GET`/`PATCH /brand-voice` / `get_brand_voice`, `update_brand_voice` | read: `create_posts` or `manage_workspace_settings`; update: `manage_workspace_settings` |
| Billing: plan, limits, usage (read-only; org owners and admins) | none | `GET /usage` / `get_usage` | read |
| Inbox, replies | yes / yes | unchanged | `use_inbox`, `reply_from_inbox` |
| Analytics | yes / yes | unchanged | `view_analytics` |
| Client reports: list, run now, latest run | none | `GET /reports`, `POST /reports/{id}/run`, `GET /reports/{id}/runs/latest` / `list_reports`, `run_report`, `get_latest_report_run`; a report covering an account outside the allowlist is hidden | `view_analytics`; run: + `manage_social_accounts`; plan feature `branded_reports` |
| Link-in-bio pages: list, read, edit, links, stats | none | `GET /bio/pages`, `GET`/`PATCH /bio/pages/{id}`, `POST /bio/pages/{id}/links`, `PATCH`/`DELETE /bio/pages/{id}/links/{link}`, `GET /bio/pages/{id}/stats` / `list_bio_pages`, `get_bio_page`, `update_bio_page`, `add_bio_link`, `update_bio_link`, `delete_bio_link`, `get_bio_stats`; only http(s) link addresses; icons and the posts grid can only name allowlisted accounts | read: `view_analytics`; edit: `manage_social_accounts` |
| AI writing assistant | yes / yes | unchanged | `create_posts` |
| Webhooks | yes / yes | unchanged | `manage_workspace_settings` or `manage_social_accounts` |

## Still web-only

Connecting or disconnecting accounts (it needs the platform's OAuth screen), editing hashtag sets and signatures, comment edit/delete and attachments, idea media, the client portal and magic links, editing members and roles, API keys, billing checkout, CSV import and RSS feeds.

## Using it

- Pagination: `limit` (default 50, max 100) and `cursor`; keep calling with `next_cursor` until it is `null`.
- Errors: `{"error": "...", "detail": "..."}` on REST; a JSON-RPC `INVALID_PARAMS` error with the same message on MCP. `403` means a missing permission or an account outside the allowlist, `404` an unknown id (or one you can't see), `409` a state conflict, `422` invalid input.
- Retries: create routes accept an `Idempotency-Key` header and create-like MCP tools an `idempotency_key` argument; the same key with the same body returns the first result for 24 hours, a different body is refused (`422`).
- Rate limits and audit: every call counts against the per-key, per-workspace and plan limits. Writes, and any failed call, leave an audit row.
- A planning loop: `get_brand_voice` and `list_calendar_events`, draft with `create_draft` (`tags`, `category_id`), `list_approvals` and `review_post`, then `add_post_to_queue` (or `find_next_free_slot` and `schedule_draft`), and `get_usage` before large batches.
