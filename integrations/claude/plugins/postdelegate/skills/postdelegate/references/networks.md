# Per-network writing notes

Limits change. Always take the live limit from `list_accounts` (`char_limit`) and from the error a tool returns. These notes are about style only.

| Network | What works | Watch for |
| --- | --- | --- |
| X | Short, one idea, a clear first line; threads for longer arguments | The limit counts links and some emoji in a special way; keep a margin. |
| LinkedIn | Plain-spoken lessons, first line as hook, short paragraphs, a question to close | Link in the first comment often reads better than in the body (`first_comment`). |
| Instagram | Caption supports the visual; hook in the first line (it is truncated); media is required | Needs an image or video (`requires_media`). Keep hashtags relevant and few. |
| Facebook | Conversational, community tone, a question or photo | Long posts are truncated behind "See more". |
| Threads | Casual, conversational, one thought | Avoid copy-pasting an X post. |
| Bluesky and Mastodon | Plain text, alt text on images, little promotion | Honour community norms around hashtags and content warnings. |
| TikTok | A hook caption and a video | Needs a video (`requires_video`); privacy level is set per account default. |
| YouTube | Searchable title, description with keywords and chapters | A title is needed (`needs_title`). |
| Pinterest | Search-style title and description, vertical image | A board must be chosen in the composer, so use `create_draft`, not `schedule_post`. |
| Google Business, Reddit, others | Follow the account's `needs_title`, media and link rules from `list_accounts` | Promotional posts are often unwelcome; ask the user. |

Cross-posting rule: write each network's version from the same idea, then adjust length, tone, link placement, and hashtags. Do not reuse an identical text on every network.
