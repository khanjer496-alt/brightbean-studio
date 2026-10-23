"""Reading the dial: which level applies to an action on an account.

A person is never subject to the dial. For agents and automations the level is
the account's override when it sets one, otherwise the workspace's.
"""

from __future__ import annotations

from django.conf import settings

from .models import AccountAutonomyOverride, ActionType, AutonomyPolicy, Level, RecycleLevel

_LEVEL_FIELD = {
    ActionType.DRAFT_CONTENT: "draft_level",
    ActionType.PUBLISH_POSTS: "publish_level",
    ActionType.REPLY: "reply_level",
    ActionType.RECYCLE_EVERGREEN: "recycle_level",
}

# What the dial does, in words a workspace owner can act on.
LEVEL_DESCRIPTIONS = {
    ActionType.DRAFT_CONTENT: {
        Level.OFF: "Agents cannot create or edit drafts at all.",
        Level.DRAFT_ONLY: "Agents may write drafts. Nothing goes out until a person approves it.",
        Level.AUTO_GUARDRAILS: "Agents may write drafts; any that trip a guardrail are flagged for a closer look.",
        Level.FULL_AUTO: "Agents may write drafts freely. Only the blocked-phrases list is checked.",
    },
    ActionType.PUBLISH_POSTS: {
        Level.OFF: "Agents cannot schedule or publish. Example: a request to schedule is refused.",
        Level.DRAFT_ONLY: "A scheduled post becomes a draft waiting for your approval.",
        Level.AUTO_GUARDRAILS: "Posts that pass every guardrail go out after the undo window. Others wait for you.",
        Level.FULL_AUTO: "Posts go out after the undo window with no checks beyond blocked phrases and crisis pause.",
    },
    ActionType.REPLY: {
        Level.OFF: "Agents cannot reply to comments or DMs, not even drafts.",
        Level.DRAFT_ONLY: "Replies are written as drafts in the inbox. A person sends them.",
        Level.AUTO_GUARDRAILS: "Replies that pass every guardrail are sent after the undo window. Others wait for you.",
        Level.FULL_AUTO: "Replies are sent after the undo window with no checks beyond blocked phrases and crisis pause.",
    },
    ActionType.RECYCLE_EVERGREEN: {
        RecycleLevel.PER_ITEM: "Each evergreen item keeps its own approval setting.",
        Level.OFF: "Nothing is recycled automatically.",
        Level.DRAFT_ONLY: "Every repeat is sent for review before it can go out.",
        Level.AUTO_GUARDRAILS: "Repeats that pass every guardrail are queued. Others are sent for review.",
        Level.FULL_AUTO: "Repeats are queued with no checks beyond blocked phrases and crisis pause.",
    },
}

# Plain examples shown beside each action on the settings page.
ACTION_EXAMPLES = {
    ActionType.DRAFT_CONTENT: "An agent writes next week's captions from your content calendar.",
    ActionType.PUBLISH_POSTS: "An agent schedules Tuesday's product announcement.",
    ActionType.REPLY: "An agent answers 'what time do you open?' under your post.",
    ActionType.RECYCLE_EVERGREEN: "Your best tip from last spring goes out again with fresh wording.",
}


def default_policy(workspace) -> AutonomyPolicy:
    """An unsaved policy holding the levels a workspace gets before anyone sets them."""
    return AutonomyPolicy(
        workspace=workspace,
        undo_delay_minutes=settings.AUTONOMY_DEFAULT_UNDO_MINUTES,
        **settings.AUTONOMY_DEFAULT_LEVELS,
    )


def get_policy(workspace) -> AutonomyPolicy:
    """The workspace's policy, or an unsaved one holding the defaults (conservative unless settings say otherwise)."""
    # Read from the database, not the reverse-relation cache: a policy changed a moment ago must apply now.
    return AutonomyPolicy.objects.filter(workspace_id=workspace.id).first() or default_policy(workspace)


def get_or_create_policy(workspace) -> AutonomyPolicy:
    """The saved policy, creating it from the defaults the first time anyone changes a setting."""
    policy = AutonomyPolicy.objects.filter(workspace_id=workspace.id).first()
    if policy is None:
        policy = default_policy(workspace)
        policy.save()
    return policy


def get_override(account) -> AccountAutonomyOverride | None:
    if account is None:
        return None
    return AccountAutonomyOverride.objects.filter(social_account_id=account.id).first()


def resolve_level(workspace, account, action_type: str, *, policy: AutonomyPolicy | None = None) -> tuple[str, str]:
    """``(level, source)``: the dial setting for ``action_type`` and where it came from."""
    policy = policy or get_policy(workspace)
    field = _LEVEL_FIELD[action_type]
    override = get_override(account)
    if override is not None and getattr(override, field):
        return getattr(override, field), "account"
    return getattr(policy, field), "workspace"


def max_posts_per_day(policy: AutonomyPolicy, account) -> int:
    override = get_override(account)
    if override is not None and override.max_posts_per_day is not None:
        return override.max_posts_per_day
    return policy.max_posts_per_day
