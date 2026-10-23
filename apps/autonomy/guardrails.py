"""Guardrails: the checks an autonomous action must pass at "Auto with guardrails".

Each check yields a :class:`GuardrailResult` with a plain-language reason, so the
decision can be shown to the person who has to review it and to the agent that
caused it. Nothing here knows about a platform: the text and the account's own
limits are all it looks at.

The keyword and pattern checks are a table of :class:`Classifier` rows. Add a
row (or call :func:`register_classifier`) to teach the system a new kind of
content that needs a person's eye.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import zoneinfo
from dataclasses import dataclass, field

from .models import ActionType, AgentAction, Decision, Level

logger = logging.getLogger(__name__)

POST_ACTIONS = (ActionType.PUBLISH_POSTS, ActionType.RECYCLE_EVERGREEN)


@dataclass
class GuardrailResult:
    name: str
    label: str
    passed: bool
    reason: str = ""
    # A blocking failure refuses the action outright; any other failure holds it for review.
    blocking: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "passed": self.passed,
            "reason": self.reason,
            "blocking": self.blocking,
        }


@dataclass
class Report:
    results: list[GuardrailResult] = field(default_factory=list)
    risk_score: float | None = None
    ai_used: bool = False

    @property
    def blocked(self) -> bool:
        return any(not r.passed and r.blocking for r in self.results)

    @property
    def needs_review(self) -> bool:
        return any(not r.passed and not r.blocking for r in self.results)

    def failures(self) -> list[GuardrailResult]:
        return [r for r in self.results if not r.passed]

    def as_dicts(self) -> list[dict]:
        return [r.as_dict() for r in self.results]


# ---------------------------------------------------------------------------
# Keyword and pattern checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classifier:
    name: str
    label: str
    # The policy field that switches the check on.
    setting: str
    pattern: re.Pattern
    reason: str


_FLAGS = re.IGNORECASE

CLASSIFIERS: list[Classifier] = [
    Classifier(
        "links",
        "Links",
        "review_links",
        re.compile(r"https?://\S+|\bwww\.\S+|\b[a-z0-9-]+\.(?:com|org|net|io|co|app|ly|me|info|biz)\b/?\S*", _FLAGS),
        "contains a link",
    ),
    Classifier(
        "mentions",
        "@mentions",
        "review_mentions",
        re.compile(r"(?<![\w@.])@[a-z0-9_][a-z0-9_.]{1,}", _FLAGS),
        "mentions another account",
    ),
    Classifier(
        "prices",
        "Prices and discounts",
        "review_prices",
        re.compile(
            r"[$€£¥]\s?\d|\b\d[\d,.]*\s?(?:usd|eur|gbp|dollars?|euros?|pounds?)\b|\b\d+\s?%\s?(?:off|discount)"
            r"|\b(?:discount|coupon|promo code|sale price|on sale|price drop|limited[- ]time offer|free shipping"
            r"|money[- ]back|% off|buy one get one)\b",
            _FLAGS,
        ),
        "makes a price or discount claim",
    ),
    Classifier(
        "health_claims",
        "Health claims",
        "review_sensitive",
        re.compile(
            r"\b(?:cures?|cured|treats?|heals?|prevents?|diagnos\w+|clinically proven|medical advice|miracle"
            r"|weight[- ]loss|detox|fda[- ]approved|lowers? (?:blood|cholesterol)|side effects?)\b",
            _FLAGS,
        ),
        "makes a health claim",
    ),
    Classifier(
        "financial_claims",
        "Financial claims",
        "review_sensitive",
        re.compile(
            r"\b(?:guaranteed returns?|risk[- ]free|get rich|double your money|investment advice|financial advice"
            r"|passive income|high[- ]yield|no risk|insider tip|returns? of \d+)\b",
            _FLAGS,
        ),
        "makes a financial claim",
    ),
    Classifier(
        "legal_claims",
        "Legal claims",
        "review_sensitive",
        re.compile(
            r"\b(?:legal advice|lawsuit|sue\b|liable|liability|guaranteed outcome|you will win your case"
            r"|not legally|breach of contract|class action)\b",
            _FLAGS,
        ),
        "makes a legal claim",
    ),
]


def register_classifier(classifier: Classifier) -> None:
    """Add a check; replaces any existing one with the same name."""
    CLASSIFIERS[:] = [c for c in CLASSIFIERS if c.name != classifier.name]
    CLASSIFIERS.append(classifier)


def blocked_phrase_hits(phrases, text: str) -> list[str]:
    """The phrases from ``phrases`` that appear in ``text`` (whole words, any case)."""
    hits = []
    for phrase in phrases or []:
        phrase = (phrase or "").strip()
        if not phrase:
            continue
        words = r"\s+".join(re.escape(word) for word in phrase.split())
        pattern = rf"(?<!\w){words}(?!\w)"
        if re.search(pattern, text or "", _FLAGS):
            hits.append(phrase)
    return hits


def check_blocked_phrases(policy, text: str) -> GuardrailResult:
    hits = blocked_phrase_hits(policy.blocked_phrases, text)
    if hits:
        quoted = ", ".join(f'"{h}"' for h in hits[:5])
        return GuardrailResult(
            "blocked_phrases", "Blocked phrases", False, f"it contains a blocked phrase: {quoted}", blocking=True
        )
    return GuardrailResult("blocked_phrases", "Blocked phrases", True)


def check_classifiers(policy, text: str) -> list[GuardrailResult]:
    results = []
    for classifier in CLASSIFIERS:
        if not getattr(policy, classifier.setting, False):
            continue
        if classifier.pattern.search(text or ""):
            results.append(GuardrailResult(classifier.name, classifier.label, False, classifier.reason))
        else:
            results.append(GuardrailResult(classifier.name, classifier.label, True))
    return results


# ---------------------------------------------------------------------------
# Limits and timing
# ---------------------------------------------------------------------------


def _local_day_bounds(workspace, when: dt.datetime):
    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    local = when.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=1)


def check_daily_cap(policy, workspace, account, action_type: str, when: dt.datetime) -> GuardrailResult:
    from .policy import max_posts_per_day

    if action_type == ActionType.REPLY:
        cap, noun, group = policy.max_replies_per_day, "replies", (ActionType.REPLY,)
    else:
        cap, noun, group = max_posts_per_day(policy, account), "posts", POST_ACTIONS
    label = "Daily limit"
    if account is None:
        return GuardrailResult("daily_cap", label, True)
    start, end = _local_day_bounds(workspace, when)
    done = (
        AgentAction.objects.filter(
            workspace=workspace,
            social_account=account,
            action_type__in=group,
            decision=Decision.EXECUTED,
            undone_at__isnull=True,
            requested_at__gte=start,
            requested_at__lt=end,
        )
        .exclude(actor_type="human")
        .count()
    )
    if done >= cap:
        return GuardrailResult(
            "daily_cap", label, False, f"the account already has {done} autonomous {noun} that day (limit {cap})"
        )
    return GuardrailResult("daily_cap", label, True)


def in_quiet_hours(policy, workspace, when: dt.datetime) -> bool:
    if not policy.quiet_hours_enabled or policy.quiet_start is None or policy.quiet_end is None:
        return False
    local = when.astimezone(zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")).time()
    start, end = policy.quiet_start, policy.quiet_end
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def check_quiet_hours(policy, workspace, when: dt.datetime) -> GuardrailResult:
    if in_quiet_hours(policy, workspace, when):
        return GuardrailResult(
            "quiet_hours",
            "Quiet hours",
            False,
            f"it would go out during quiet hours ({policy.quiet_start:%H:%M} to {policy.quiet_end:%H:%M})",
        )
    return GuardrailResult("quiet_hours", "Quiet hours", True)


# ---------------------------------------------------------------------------
# Optional AI checks
# ---------------------------------------------------------------------------


def check_ai(policy, workspace, text: str, account, *, source: str) -> tuple[list[GuardrailResult], float | None]:
    """The AI risk and brand voice checks, when switched on. A check that can't run holds the action."""
    from apps.ai_writer.client import AIError
    from apps.ai_writer.models import AIRequestLog
    from apps.ai_writer.services import AIContext, review_content
    from apps.billing.entitlements import PlanLimitError

    wants_risk, wants_voice = policy.ai_risk_check, policy.ai_voice_check
    if not (wants_risk or wants_voice):
        return [], None
    src = AIRequestLog.Source.MCP if source == "mcp" else AIRequestLog.Source.API
    if source == "system":
        src = AIRequestLog.Source.SYSTEM
    ctx = AIContext(workspace=workspace, user=None, source=src)
    try:
        review = review_content(ctx, text, account, check_voice=wants_voice)
    except (AIError, PlanLimitError, ValueError) as exc:
        message = getattr(exc, "message", None) or str(exc) or "the AI service is unavailable"
        logger.info("Autonomy AI check could not run for workspace %s: %s", workspace.pk, message)
        reason = f"the AI check could not run ({message})"
        results = []
        if wants_risk:
            results.append(GuardrailResult("ai_risk", "AI risk check", False, reason))
        if wants_voice:
            results.append(GuardrailResult("brand_voice", "Brand voice", False, reason))
        return results, None

    results = []
    score = review["risk_score"]
    if wants_risk:
        if score >= policy.ai_risk_threshold:
            why = "; ".join(review["risk_reasons"]) or "the reviewer flagged it"
            results.append(
                GuardrailResult(
                    "ai_risk",
                    "AI risk check",
                    False,
                    f"AI risk score {score:.2f} (limit {policy.ai_risk_threshold:.2f}): {why}",
                )
            )
        else:
            results.append(GuardrailResult("ai_risk", "AI risk check", True, f"AI risk score {score:.2f}"))
    if wants_voice:
        drift = review["voice_drift"]
        if drift >= policy.ai_voice_threshold:
            why = "; ".join(review["voice_reasons"]) or "it does not sound like the brand"
            results.append(
                GuardrailResult(
                    "brand_voice", "Brand voice", False, f"it drifts from the brand voice ({drift:.2f}): {why}"
                )
            )
        else:
            results.append(GuardrailResult("brand_voice", "Brand voice", True, f"voice drift {drift:.2f}"))
    return results, score


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    policy,
    *,
    workspace,
    account,
    action_type: str,
    text: str,
    when: dt.datetime,
    level: str,
    source: str = "api",
) -> Report:
    """Evaluate the guardrails that apply at ``level`` ("Full auto" keeps only the blocked phrases)."""
    report = Report()
    report.results.append(check_blocked_phrases(policy, text))
    if level != Level.AUTO_GUARDRAILS:
        return report
    report.results.extend(check_classifiers(policy, text))
    if action_type in (ActionType.PUBLISH_POSTS, ActionType.RECYCLE_EVERGREEN, ActionType.REPLY):
        report.results.append(check_daily_cap(policy, workspace, account, action_type, when))
        if policy.quiet_hours_enabled:
            report.results.append(check_quiet_hours(policy, workspace, when))
    ai_results, score = check_ai(policy, workspace, text, account, source=source)
    if ai_results:
        report.results.extend(ai_results)
        report.ai_used = True
        report.risk_score = score
    return report
