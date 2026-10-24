# PostDelegate plugin for Claude

One connector (the PostDelegate remote MCP server) and one skill (`postdelegate`) that teaches Claude to plan, draft, schedule and review social posts safely.

Install from the marketplace:

    /plugin install postdelegate --marketplace <owner>/<repo>

Or in a shell:

    claude plugin marketplace add <owner>/<repo>
    claude plugin install postdelegate@postdelegate

On first use Claude opens the PostDelegate sign-in. You choose what Claude may do on the approval screen; the workspace's autonomy setting decides whether posts go out or are held for review.

Needs a PostDelegate account: https://app.postdelegate.com. Privacy: https://app.postdelegate.com/legal/privacy/. Support: support@postdelegate.com.
