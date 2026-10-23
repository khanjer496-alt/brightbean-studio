"""Put the new "Get started" checklist away for people already using the product.

The checklist gained items (brand voice, posting times, the AI writer), so a
long-time owner who had finished the old list would see it come back. Owners
of a workspace that has already scheduled or published a post get it
dismissed; everyone still getting started keeps it.
"""

from django.db import migrations
from django.utils import timezone

_ESTABLISHED = ["scheduled", "publishing", "published", "failed"]
_BATCH = 1000


def dismiss_for_established(apps, schema_editor):
    OnboardingChecklist = apps.get_model("onboarding", "OnboardingChecklist")
    PlatformPost = apps.get_model("composer", "PlatformPost")
    WorkspaceMembership = apps.get_model("members", "WorkspaceMembership")

    workspace_ids = PlatformPost.objects.filter(status__in=_ESTABLISHED).values("post__workspace_id")
    owners = (
        WorkspaceMembership.objects.filter(workspace_role="owner", workspace_id__in=workspace_ids)
        .values_list("user_id", "workspace_id")
        .iterator(chunk_size=_BATCH)
    )
    now = timezone.now()
    batch = []
    for user_id, workspace_id in owners:
        batch.append((user_id, workspace_id))
        if len(batch) >= _BATCH:
            _dismiss(OnboardingChecklist, batch, now)
            batch = []
    if batch:
        _dismiss(OnboardingChecklist, batch, now)


def _dismiss(OnboardingChecklist, pairs, now):
    OnboardingChecklist.objects.bulk_create(
        [
            OnboardingChecklist(user_id=user_id, workspace_id=workspace_id, is_dismissed=True, dismissed_at=now)
            for user_id, workspace_id in pairs
        ],
        update_conflicts=True,
        unique_fields=["user", "workspace"],
        update_fields=["is_dismissed", "dismissed_at"],
    )


class Migration(migrations.Migration):
    dependencies = [
        ("onboarding", "0002_welcome_flow_hints_samples"),
        ("composer", "0027_hashtag_sets_and_signatures"),
        ("members", "0003_invitation_last_sent_at_invitation_send_count"),
    ]

    operations = [
        migrations.RunPython(dismiss_for_established, migrations.RunPython.noop),
    ]
