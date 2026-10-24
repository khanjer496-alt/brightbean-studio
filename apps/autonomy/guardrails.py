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
import unicodedata
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


# Letters from other scripts that render like Latin ones, so "frее" (Cyrillic е) can't slip past
# a blocked "free". Only lookalikes of Latin letters are folded; real Arabic, Greek or Cyrillic words
# stay as written (a phrase typed in those scripts is folded the same way, so it still matches).
_CONFUSABLES = str.maketrans(
    {
        # Cyrillic
        "а": "a",
        "в": "b",
        "е": "e",
        "ё": "e",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "і": "i",
        "ї": "i",
        "ј": "j",
        "ѕ": "s",
        "ԁ": "d",
        "ӏ": "l",
        "ɡ": "g",
        # Greek
        "α": "a",
        "β": "b",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "ζ": "z",
        "η": "n",
        # Punctuation that stands in for a dot or an at sign
        "。": ".",
        "｡": ".",
        "․": ".",
        "‧": ".",
        "﹫": "@",
        # Arabic letter variants written interchangeably
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ة": "ه",
        "ؤ": "و",
        "ئ": "ي",
    }
)


def normalize(text: str) -> str:
    """Text as the checks read it, so look-alike spellings can't dodge them.

    NFKC folds full-width and styled letters (𝐟𝐫𝐞𝐞, ｆｒｅｅ) and Arabic presentation
    forms; invisible format characters (zero-width spaces and joiners, bidi marks,
    soft hyphens), combining marks (accents, strike-throughs, Arabic diacritics) and
    the Arabic tatweel are dropped; homoglyphs of Latin letters are folded; case is
    folded; runs of whitespace become one space. Phrases and text go through the
    same folding, so a phrase typed with accents or in another script still matches.
    """
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text or ""))
    kept = [ch for ch in text if unicodedata.category(ch) not in ("Cf", "Mn") and ch != "\u0640"]
    text = unicodedata.normalize("NFC", "".join(kept)).casefold().translate(_CONFUSABLES)
    return " ".join(text.split())


@dataclass(frozen=True)
class Classifier:
    name: str
    label: str
    # The policy field that switches the check on.
    setting: str
    pattern: re.Pattern
    reason: str


_FLAGS = re.IGNORECASE


def _terms(*terms: str) -> str:
    """An alternation of plain terms, folded like the text they are matched against.

    Used for Arabic, which attaches prefixes (و، ال، ب) to words, so the terms
    match inside a word rather than at a word boundary.
    """
    return "(?:" + "|".join(re.escape(normalize(t)) for t in terms) + ")"


def _pattern(english: str, *arabic: str) -> re.Pattern:
    return re.compile(english + ("|" + _terms(*arabic) if arabic else ""), _FLAGS)


CLASSIFIERS: list[Classifier] = [
    Classifier(
        "links",
        "Links",
        "review_links",
        re.compile(
            r"https?://\S+|\bwww\.\S+|\b[a-z0-9-]+\.(?:com|org|net|io|co|app|ly|me|info|biz|shop|store|link|sa|ae)\b/?\S*",
            _FLAGS,
        ),
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
        _pattern(
            r"[$€£¥₹💲💰🤑]\s?\d|\b\d[\d,.]*\s?(?:usd|eur|gbp|aed|sar|egp|kwd|qar|dollars?|euros?|pounds?)\b"
            r"|\b\d+\s?%\s?(?:off|discount)"
            r"|\b(?:discount|coupon|promo code|sale price|on sale|price drop|limited[- ]time offer|free shipping"
            r"|money[- ]back|% off|buy one get one)\b",
            "ريال",
            "ر.س",
            "درهم",
            "د.إ",
            "دينار",
            "جنيه",
            "ليرة",
            "خصم",
            "تخفيض",
            "كوبون",
            "كود الخصم",
            "عرض خاص",
            "عرض لفترة محدودة",
            "شحن مجاني",
            "السعر",
            "بسعر",
        ),
        "makes a price or discount claim",
    ),
    Classifier(
        "health_claims",
        "Health claims",
        "review_sensitive",
        _pattern(
            r"\b(?:cures?|cured|cure for|treatment for|heals?|diagnos\w+|clinically proven|medical advice|miracle"
            r"|weight[- ]loss|detox|fda[- ]approved|lowers? (?:blood|cholesterol)|side effects?|immune boost\w*"
            r"|anti[- ]aging)\b",
            "يعالج",
            "علاج",
            "يشفي",
            "شفاء",
            "تخسيس",
            "إنقاص الوزن",
            "خسارة الوزن",
            "ديتوكس",
            "مثبت طبيا",
            "مثبت علميا",
            "نصيحة طبية",
            "أعراض جانبية",
            "يقوي المناعة",
        ),
        "makes a health claim",
    ),
    Classifier(
        "financial_claims",
        "Financial claims",
        "review_sensitive",
        _pattern(
            r"\b(?:guaranteed returns?|guaranteed profits?|risk[- ]free|get rich|double your money|investment advice"
            r"|financial advice|passive income|high[- ]yield|no risk|insider tip|returns? of \d+)\b",
            "أرباح مضمونة",
            "ربح مضمون",
            "عائد مضمون",
            "بدون مخاطر",
            "بدون مخاطرة",
            "دخل سلبي",
            "نصيحة استثمارية",
            "نصيحة مالية",
            "ضاعف أموالك",
            "اربح من البيت",
        ),
        "makes a financial claim",
    ),
    Classifier(
        "legal_claims",
        "Legal claims",
        "review_sensitive",
        _pattern(
            r"\b(?:legal advice|lawsuit|sue (?:you|them|us|him|her)|liable|liability|guaranteed outcome"
            r"|you will win your case|not legally|breach of contract|class action)\b",
            "استشارة قانونية",
            "نصيحة قانونية",
            "دعوى قضائية",
            "مسؤولية قانونية",
            "رفع قضية",
            "ضمان كسب القضية",
        ),
        "makes a legal claim",
    ),
]


def register_classifier(classifier: Classifier) -> None:
    """Add a check; replaces any existing one with the same name."""
    CLASSIFIERS[:] = [c for c in CLASSIFIERS if c.name != classifier.name]
    CLASSIFIERS.append(classifier)


def blocked_phrase_hits(phrases, text: str) -> list[str]:
    """The phrases from ``phrases`` that appear in ``text``.

    Both sides are folded by :func:`normalize` first, so case, accents, zero-width
    characters, full-width or styled letters and look-alike letters from other
    scripts make no difference. A phrase in a script that marks word boundaries
    with spaces matches whole words; one containing Arabic matches anywhere, as
    Arabic attaches prefixes to the word.
    """
    folded = normalize(text)
    hits = []
    for phrase in phrases or []:
        key = normalize(phrase)
        if not key:
            continue
        words = r"\s+".join(re.escape(word) for word in key.split())
        pattern = words if _ARABIC_RE.search(key) else rf"(?<!\w){words}(?!\w)"
        if re.search(pattern, folded):
            hits.append(phrase.strip())
    return hits


_ARABIC_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f]")


def check_blocked_phrases(policy, text: str) -> GuardrailResult:
    hits = blocked_phrase_hits(policy.blocked_phrases, text)
    if hits:
        quoted = ", ".join(f'"{h}"' for h in hits[:5])
        return GuardrailResult(
            "blocked_phrases", "Blocked phrases", False, f"it contains a blocked phrase: {quoted}", blocking=True
        )
    return GuardrailResult("blocked_phrases", "Blocked phrases", True)


def check_classifiers(policy, text: str) -> list[GuardrailResult]:
    folded = normalize(text)
    results = []
    for classifier in CLASSIFIERS:
        if not getattr(policy, classifier.setting, False):
            continue
        if classifier.pattern.search(folded):
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


def _ai_checks(policy):
    return (("ai_risk", "AI risk check", policy.ai_risk_check), ("brand_voice", "Brand voice", policy.ai_voice_check))


def check_ai(policy, workspace, text: str, account, *, source: str) -> tuple[list[GuardrailResult], float | None]:
    """The AI risk and brand voice checks, when switched on. A check that can't run holds the action."""
    from apps.ai_writer.client import AIError
    from apps.ai_writer.models import AIRequestLog
    from apps.ai_writer.services import MAX_CAPTION_CHARS, AIContext, review_content
    from apps.billing.entitlements import PlanLimitError

    wants_risk, wants_voice = policy.ai_risk_check, policy.ai_voice_check
    if not (wants_risk or wants_voice):
        return [], None
    if len(text or "") > MAX_CAPTION_CHARS:
        # The reviewer reads at most this much; what it can't read must not go out unread.
        reason = f"it is longer than the AI check can read in full ({MAX_CAPTION_CHARS} characters)"
        results = [GuardrailResult(name, label, False, reason) for name, label, on in _ai_checks(policy) if on]
        return results, None
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
