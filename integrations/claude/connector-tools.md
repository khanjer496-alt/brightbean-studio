# PostDelegate MCP tools

Generated from the tool registry (`apps/mcp`). Annotations come from the capability registry (`apps/api_keys/capabilities.py`). Regenerate after adding a tool.

| Tool | Capability | Read-only | Destructive | Open-world | Description |
|---|---|---|---|---|---|
| `accept_repurpose_outputs` | draft_posts | no | no | no | Save chosen outputs of a repurposing job as draft posts (one per account; thread parts included), ideas (scripts, newsletter blurbs) or a hashtag set. |
| `adapt_caption` | use_ai_assistant | no | no | no | Rewrite a caption for each account with the AI writing assistant: each network's style, the workspace's brand voice, and within each account's character limit. |
| `add_bio_link` | write_settings | no | no | no | Add a link button to the end of a link-in-bio page. |
| `add_competitor` | write_settings | no | no | no | Track a public competitor profile. |
| `add_feed` | schedule_posts | no | no | yes | Follow an RSS or Atom feed (its address is fetched and checked first; an address already followed is returned as is). |
| `add_post_comment` | manage_approvals | no | no | no | Comment on a post, optionally replying to a top-level comment (parent_id). |
| `add_post_to_queue` | schedule_posts | no | no | yes | Schedule an existing draft or approved post into a queue's next free posting slot — the way to keep an account posting on its usual rhythm without choosing times yourself. |
| `add_to_evergreen` | schedule_posts | no | no | yes | Add content to the evergreen library so it is re-posted into empty queue slots: give exactly one of post_id (a published post; one item per published channel, or only social_account_id), template_id or idea_id (both need social_account_id). |
| `apply_best_times` | schedule_posts | no | yes | no | Make an account's best times its weekly posting slots. |
| `cancel_post` | schedule_posts | no | yes | no | Cancel a scheduled post, transitioning it back to draft. |
| `cancel_repurpose_job` | use_ai_assistant | no | yes | no | Stop a queued repurposing job at once, or a running one before its next AI request. |
| `check_feed` | schedule_posts | no | no | yes | Read a feed now instead of waiting for the next check (at most once a minute) and handle its new items. |
| `create_account_set` | write_settings | no | no | no | Save a group of accounts under a name so later requests can name them with one set_id. |
| `create_calendar_event` | draft_posts | no | no | no | Add a custom event bar to the calendar (end_date defaults to start_date; `color` is #RRGGBB). |
| `create_category` | draft_posts | no | no | no | Create a content category; 409 if the name exists. |
| `create_draft` | draft_posts | no | no | no | Create a draft post against a connected account. |
| `create_idea` | draft_posts | no | no | no | Capture a content idea on the kanban board (first column unless group_id is given). |
| `create_idea_group` | draft_posts | no | no | no | Add a column to the ideas kanban board (placed last). |
| `create_plug_rule` | schedule_posts | no | no | yes | Add an engagement automation: when the post (post_id on social_account_id, or every new post of the account when post_id is omitted) reaches `threshold` likes, reposts or views within `window_hours` of publishing, `action` it: repost (boost |
| `create_posting_slot` | schedule_posts | no | no | no | Add a weekly posting slot to an account (e.g. |
| `create_reply_draft` | reply_inbox | no | no | no | Draft a reply to an inbox message. |
| `create_repurpose_job` | use_ai_assistant | no | no | yes | Turn one source into several outputs with the AI writer: threads (accounts that take threads), long-form posts, carousels (caption + slide texts; a branded PDF is saved to the media library for networks that take PDF carousels), standalone  |
| `create_set_draft` | draft_posts | no | no | no | Write one draft post on every connected account of a saved set, applying each account's saved default settings (a Pinterest board, a TikTok privacy level). |
| `create_short_link` | draft_posts | no | no | no | Make a short link to an http(s) address with the workspace's link service (built in by default; the workspace may have set up its own Dub, Short.io or Kutt account). |
| `create_tag` | draft_posts | no | no | no | Create a tag, or return the existing one with that name. |
| `create_template` | draft_posts | no | no | no | Save a reusable post template. |
| `create_webhook_endpoint` | write_settings | no | no | yes | Create an outbound webhook endpoint. |
| `delete_account_set` | write_settings | no | yes | no | Delete a saved account set; its accounts stay connected. |
| `delete_bio_link` | write_settings | no | yes | no | Remove a link from a link-in-bio page (its past click counts stay in the stats). |
| `delete_calendar_event` | draft_posts | no | yes | no | Delete a custom calendar event (find its id with list_calendar_events). |
| `delete_category` | draft_posts | no | yes | no | Delete a content category; posts that used it are left uncategorised. |
| `delete_idea` | draft_posts | no | yes | no | Permanently delete an idea from the kanban board (find its id with list_ideas). |
| `delete_idea_group` | draft_posts | no | yes | no | Delete an empty kanban column; refused (409) while it still holds ideas — move or delete them first. |
| `delete_plug_rule` | schedule_posts | no | yes | no | Remove an engagement rule; waiting watches end with it, what it already did stays. |
| `delete_post` | schedule_posts | no | yes | no | Permanently delete a draft, approved or scheduled post (a series' first post also removes its upcoming copies). |
| `delete_posting_slot` | schedule_posts | no | yes | no | Delete a posting slot. |
| `delete_tag` | draft_posts | no | yes | no | Delete a tag and remove it from the posts and ideas that carry it. |
| `delete_template` | draft_posts | no | yes | no | Permanently delete a post template (find its id with list_templates). |
| `delete_webhook_endpoint` | write_settings | no | yes | no | Delete a webhook endpoint and its delivery log. |
| `discard_reply_draft` | reply_inbox | no | yes | no | Delete a draft (or failed) reply. |
| `duplicate_post` | draft_posts | no | no | no | Copy a post into a new draft: caption, title, first comment, media, thread parts, tags, category and per-platform settings, with no schedule. |
| `finalize_media_upload` | upload_media | no | no | no | Step 2 of a presigned upload: call with the 'upload_id' from request_media_upload once the bytes are uploaded. |
| `find_next_free_slot` | read_calendar | yes | no | no | Find an account's next open posting slot(s): upcoming slot times that no scheduled post already holds, soonest first (UTC). |
| `generate_caption` | use_ai_assistant | no | no | no | Write a social media caption with the workspace's AI writing assistant, in its brand voice. |
| `get_account_analytics` | read_analytics | yes | no | no | Read a channel's analytics summary over a rolling window: hero KPI metrics (views/likes/reach/etc.), an engagement-rate card when the platform supports it, and follower growth. |
| `get_account_set` | read_calendar | yes | no | no | Read one saved account set: its accounts and their default settings. |
| `get_ai_visibility_report` | read_analytics | yes | no | no | Read the latest 'how AI describes your brand' report: the share of the workspace's category prompts for which an AI model named the brand, per-prompt answers (position, tone, who else was named, web addresses), the gaps and suggested post i |
| `get_autonomy_policy` | read_settings | yes | no | no | Read how much this workspace lets you do on your own: a level per action (off, draft_only, auto_guardrails, full_auto) for drafting, publishing, replying and recycling evergreen, per-account overrides, the guardrails (blocked phrases, links |
| `get_best_times` | read_calendar | yes | no | no | Get when an account's posts do best: its top weekday and hour combinations (day_of_week 0=Monday…6=Sunday, wall-clock time in the workspace timezone) and the 7x24 score grid behind them. |
| `get_bio_page` | read_settings | yes | no | no | Read one link-in-bio page with its links (in order, with whether each shows right now). |
| `get_bio_stats` | read_analytics | yes | no | no | Page views and link clicks for a link-in-bio page over the last `days` UTC days (default 30, at most 90): totals, a per-day series and clicks per link. |
| `get_brand_kit` | read_settings | yes | no | no | Read the workspace's brand kit: logo media ids (primary, mark, on_light, on_dark), named colour swatches, fonts, tagline, default hashtags, legal disclaimers, and do's and don'ts. |
| `get_brand_voice` | read_settings | yes | no | no | Read the workspace's brand voice — tone, words to use, words to avoid and up to three example posts — which the AI writing tools apply. |
| `get_competitor_comparison` | read_analytics | yes | no | no | Compare the workspace's own accounts with the tracked competitors, per network: followers, follower growth over the range (default last 30 days), posts per week and average engagements per post. |
| `get_feed` | read_calendar | yes | no | no | Read one feed with the latest items and what became of each: scheduled, draft, held for approval, skipped (with the filter that skipped it), failed, plus whether the AI wrote it. |
| `get_idea` | read_calendar | yes | no | no | Read one idea (title, description, tags, kanban column and position) by id; find ids with list_ideas. |
| `get_inbox_message` | read_inbox | yes | no | yes | Retrieve one inbox message by ID, including its full reply thread and any draft replies. |
| `get_latest_report_run` | read_analytics | yes | no | no | Read a report's most recent run: status, period and, once ready, its headline figures and summary text. |
| `get_link_stats` | read_analytics | yes | no | no | Link clicks over the last `days` UTC days (default 30, at most 90): totals, a per-day series and clicks per link, post and account. |
| `get_media` | upload_media | yes | no | no | Retrieve a single media asset by id. |
| `get_plug_rule` | read_calendar | yes | no | no | Read one engagement rule with how it has gone on each post it watched: status (watching, queued, awaiting_approval, done, failed, blocked, expired, cancelled, unconfirmed), the count that triggered it, the platform id of what it made and an |
| `get_post` | read_calendar | yes | no | no | Retrieve a post by ID, including aggregate status and per-platform child state. |
| `get_post_analytics` | read_analytics | yes | no | no | Read a post's analytics, broken down per platform. |
| `get_queue` | read_calendar | yes | no | no | Read one queue with its entries (post, position and the publish time each was given), earliest first. |
| `get_repurpose_job` | use_ai_assistant | yes | no | no | Read a repurposing job: status (queued, running, succeeded, failed, cancelled), AI requests used, and its outputs with text, parts, per-network character counts and warnings. |
| `get_template` | read_calendar | yes | no | no | Read one post template by id (caption, first comment, tags, category, target accounts and media); find ids with list_templates. |
| `get_usage` | read_calendar | yes | no | no | Read the organization's plan, its limits and current usage (channels, workspaces, scheduled posts this month, API & MCP calls today, AI requests today; limit null = unlimited). |
| `get_workspace_analytics` | read_analytics | yes | no | no | Read analytics across every channel this API key can see, over a date range: headline totals (followers, new followers, views/impressions, engagements, engagement rate, posts published), a per-network and per-account breakdown, daily totals |
| `list_account_sets` | read_calendar | yes | no | no | List the workspace's saved account sets (groups such as 'Client A: all channels') with the members this key can use. |
| `list_accounts` | read_calendar | yes | no | no | List the social media accounts this API key is allowed to act on. |
| `list_agent_actions` | read_settings | yes | no | no | List the audit trail, newest first: who acted (human, agent or system), what, the policy's decision (executed, held for review, blocked) with the guardrail results and reasons, and links to the post or reply. |
| `list_approvals` | manage_approvals | yes | no | no | List posts in the approval workflow, soonest first. |
| `list_bio_pages` | read_settings | yes | no | no | List the workspace's link-in-bio pages (public at /l/<slug>/) with their links, theme and settings. |
| `list_calendar_events` | read_calendar | yes | no | no | List custom calendar events (product launches, campaigns, holidays) overlapping date_from…date_to (YYYY-MM-DD, inclusive, both optional), soonest first. |
| `list_categories` | read_calendar | yes | no | no | List the workspace's content categories (id, name, colour, order), used to file posts and queues. |
| `list_competitors` | read_analytics | yes | no | no | List the public competitor profiles this workspace tracks (latest followers, posting rate and average engagement per post) and which networks can be benchmarked, with the limit per network on the plan. |
| `list_evergreen_items` | read_calendar | yes | no | no | List the evergreen library: content that is re-posted into empty queue slots, with its category, minimum interval in days, repeat count and limit, whether it is paused and when it last went out. |
| `list_feeds` | read_calendar | yes | no | no | List the RSS/Atom feeds the workspace follows with their auto-posting settings (accounts, mode, destination, keyword filters, daily limit, last check and any error). |
| `list_hashtag_sets` | read_calendar | yes | no | no | List the workspace's saved hashtag groups; append a set's hashtags to a caption before creating a post. |
| `list_idea_groups` | read_calendar | yes | no | no | List the kanban columns of the ideas board with how many ideas each holds. |
| `list_ideas` | read_calendar | yes | no | no | List ideas from the content-ideas kanban board, in board order. |
| `list_inbox_messages` | read_inbox | yes | no | yes | List inbound inbox items (comments, mentions, DMs, reviews) for the accounts this API key is allowed to act on, newest first. |
| `list_members` | read_calendar | yes | no | no | List the workspace's team members with their role and the @mention handle that notifies them from add_post_comment. |
| `list_plug_rules` | read_calendar | yes | no | no | List engagement automations ('plugs') and what each allowed account's network supports. |
| `list_post_comments` | manage_approvals | yes | no | no | Read the comment threads on a post (top-level comments with their replies, oldest first). |
| `list_posting_slots` | read_calendar | yes | no | no | List the weekly posting slots (day of week 0=Monday…6=Sunday, wall-clock time in the workspace timezone) of the accounts this key can act on, optionally for one account. |
| `list_posts` | read_calendar | yes | no | no | List and search posts in this API key's workspace, newest first (by creation time). |
| `list_queues` | read_calendar | yes | no | no | List the publishing queues on the accounts this key can act on, each with its entry count and next free posting slot. |
| `list_reports` | read_analytics | yes | no | no | List the workspace's client reports (name, range, sections, schedule, recipient count, latest run), A-Z. |
| `list_short_links` | read_analytics | yes | no | no | List short links, newest first, with their clicks over the last 30 days. |
| `list_signatures` | read_calendar | yes | no | no | List the workspace's shared sign-offs (personal signatures are not included); append one to a caption. |
| `list_tags` | read_calendar | yes | no | no | List the workspace's tags, A-Z, optionally filtered by a name substring. |
| `list_templates` | read_calendar | yes | no | no | List the workspace's reusable post templates (name, caption, first comment, tags, category, target accounts and media), A-Z. |
| `list_webhook_deliveries` | read_settings | yes | no | no | List an endpoint's recent deliveries, newest first, with status code, duration, attempt count, the request payload and a truncated response. |
| `list_webhook_endpoints` | read_settings | yes | no | no | List the workspace's outbound webhook endpoints (never the secrets). |
| `pause_evergreen_item` | schedule_posts | no | yes | no | Pause an evergreen item so it is skipped when queues are filled, or resume it with paused false. |
| `remove_competitor` | write_settings | no | yes | no | Stop tracking a competitor and delete its history. |
| `remove_from_evergreen` | schedule_posts | no | yes | no | Take an item out of the evergreen library. |
| `remove_from_queue` | schedule_posts | no | yes | no | Take a post out of a queue by its queue entry id (see get_queue). |
| `rename_tag` | draft_posts | no | yes | no | Rename a tag everywhere it is used (posts and ideas); 409 if the new name exists. |
| `request_media_upload` | upload_media | no | no | no | Step 1 of uploading large media (video, or any file >1 MB) over MCP — no REST API key required. |
| `resend_webhook_delivery` | write_settings | no | no | yes | Send a delivery again: same payload, same delivery id, fresh retry schedule. |
| `resume_thread` | schedule_posts | no | no | yes | Resume a thread that stopped part-way (a platform post whose thread_status is 'partial'). |
| `review_post` | manage_approvals | no | yes | yes | Decide a post that is waiting for review, as the person behind this key (find them with list_approvals). |
| `rotate_webhook_secret` | write_settings | no | yes | no | Replace a webhook endpoint's signing secret. |
| `run_report` | write_settings | no | no | yes | Build a report now. |
| `schedule_draft` | schedule_posts | no | no | yes | Schedule an EXISTING draft or approved post — transitions eligible children to scheduled at the given UTC timestamp. |
| `schedule_post` | schedule_posts | no | no | yes | Create a post and schedule it to publish at a specific UTC timestamp. |
| `search_media` | upload_media | yes | no | no | Find media assets already uploaded to this workspace. |
| `send_reply` | reply_inbox | no | yes | yes | Deliver a reply to an inbox message's platform. |
| `send_webhook_test` | write_settings | no | no | yes | Queue a `webhook.test` ping to an endpoint (even a turned-off one). |
| `stop_feed_autopost` | schedule_posts | no | yes | no | Stop auto-posting a feed. |
| `update_account_set` | write_settings | no | yes | no | Rename a set, replace its accounts or its default settings; omitted fields are unchanged. |
| `update_autonomy_policy` | write_settings | no | yes | no | Make the autonomy policy stricter: lower a level, turn crisis_paused on or add blocked_phrases. |
| `update_bio_link` | write_settings | no | yes | no | Edit a link on a link-in-bio page (omitted fields are unchanged), or pass move: up / down to swap it with its neighbour. |
| `update_bio_page` | write_settings | no | yes | no | Edit a link-in-bio page; omitted fields are unchanged. |
| `update_brand_kit` | write_settings | no | yes | no | Change the workspace's brand kit. |
| `update_brand_voice` | write_settings | no | yes | no | Change the brand voice (tone, words_to_use, words_to_avoid: up to 1,000 characters each; examples: up to 3 posts of 2,000 characters, replacing the list). |
| `update_category` | draft_posts | no | yes | no | Rename, recolour or reorder a content category; omitted fields are unchanged. |
| `update_feed_autopost` | schedule_posts | no | yes | yes | Switch RSS auto-posting on for a feed, or change it. |
| `update_idea` | draft_posts | no | yes | no | Edit an idea or move it on the board: change title, description or tags, or set group_id / position to move it. |
| `update_plug_rule` | schedule_posts | no | yes | yes | Change an engagement rule (trigger, threshold, window_hours, action, message, reposter) or switch it on or off with is_active; omitted fields are unchanged. |
| `update_post` | draft_posts | no | yes | no | Edit an existing draft, approved or scheduled post (REST PATCH /posts/{id}); omitted fields are unchanged. |
| `update_posting_slot` | schedule_posts | no | yes | no | Change a posting slot's time and/or switch it on or off (inactive slots are skipped by queues). |
| `update_reply_draft` | reply_inbox | no | yes | no | Replace the body of an existing draft (or failed) reply. |
| `update_template` | draft_posts | no | yes | no | Edit a post template; omitted fields are unchanged and platform_account_ids replaces the target accounts (all must be in the key's allowlist). |
| `update_webhook_endpoint` | write_settings | no | yes | yes | Change a webhook endpoint's url, event_types, description or is_enabled. |
| `upload_media` | upload_media | no | no | yes | Upload a small media file (≤1 MB raw / ~1.3 MB base64) via base64. |
