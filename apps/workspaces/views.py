from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.billing.entitlements import PlanLimitError, check_can_add_workspace, require_feature
from apps.billing.plans import Feature
from apps.composer.models import HashtagSet, Signature
from apps.composer.services import MAX_HASHTAGS_PER_SET, parse_hashtags
from apps.members.decorators import require_org_role
from apps.members.models import OrgMembership, WorkspaceMembership

from . import brand_kit, brand_kit_forms
from .models import BrandKit, Workspace


@login_required
def workspace_list(request):
    memberships = WorkspaceMembership.objects.filter(user=request.user).select_related("workspace")
    workspaces = [m.workspace for m in memberships if not m.workspace.is_archived]
    return render(request, "workspaces/list.html", {"workspaces": workspaces})


@login_required
@require_POST
@require_org_role(OrgMembership.OrgRole.ADMIN)
def workspace_create(request):
    """Create a new workspace in the user's organization."""
    name = request.POST.get("name", "").strip()
    if not name:
        return redirect("dashboard")

    if not request.org:
        return redirect("dashboard")

    try:
        check_can_add_workspace(request.org)
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return redirect("organizations:workspaces")

    workspace = Workspace.objects.create(
        organization=request.org,
        name=name,
    )

    WorkspaceMembership.objects.create(
        user=request.user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )

    # Set as current workspace
    request.user.last_workspace_id = workspace.id
    request.user.save(update_fields=["last_workspace_id"])

    return redirect("calendar:calendar", workspace_id=workspace.id)


@login_required
@require_http_methods(["GET", "POST"])
def workspace_settings(request, workspace_id):
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise Http404 from None

    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if not membership:
        raise Http404

    is_owner_or_manager = membership.workspace_role in (
        WorkspaceMembership.WorkspaceRole.OWNER,
        WorkspaceMembership.WorkspaceRole.MANAGER,
    )

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "archive_workspace" and is_owner_or_manager:
            with transaction.atomic():
                active_count = (
                    Workspace.objects.select_for_update()
                    .filter(organization=workspace.organization, is_archived=False)
                    .count()
                )
                if active_count <= 1:
                    messages.error(request, "Cannot archive the last workspace in the organization.")
                    return redirect("workspaces:settings", workspace_id=workspace.id)
                workspace.is_archived = True
                workspace.save(update_fields=["is_archived"])
            messages.success(request, f'Workspace "{workspace.name}" has been archived.')
            return redirect("organizations:workspaces")

        if action == "unarchive_workspace" and is_owner_or_manager:
            try:
                check_can_add_workspace(workspace.organization)
            except PlanLimitError as exc:
                messages.error(request, exc.message)
                return redirect("workspaces:settings", workspace_id=workspace.id)
            workspace.is_archived = False
            workspace.save(update_fields=["is_archived"])
            messages.success(request, f'Workspace "{workspace.name}" has been restored.')
            return redirect("workspaces:settings", workspace_id=workspace.id)

        if action == "delete_workspace" and is_owner_or_manager:
            with transaction.atomic():
                active_count = (
                    Workspace.objects.select_for_update()
                    .filter(organization=workspace.organization, is_archived=False)
                    .count()
                )
                if active_count <= 1 and not workspace.is_archived:
                    messages.error(request, "Cannot delete the last workspace in the organization.")
                    return redirect("workspaces:settings", workspace_id=workspace.id)
                workspace_name = workspace.name
                workspace.delete()
            messages.success(request, f'Workspace "{workspace_name}" has been permanently deleted.')
            return redirect("organizations:workspaces")

        name = request.POST.get("name", "").strip()

        if name:
            workspace.name = name

        # Handle logo deletion
        if request.POST.get("delete_icon") == "1":
            if workspace.icon:
                workspace.icon.delete(save=False)
        # Handle logo upload
        elif "icon" in request.FILES:
            icon = request.FILES["icon"]

            # Validate file type
            allowed_types = ("image/jpeg", "image/png", "image/webp", "image/gif")
            if icon.content_type not in allowed_types:
                messages.error(request, "Logo must be a JPEG, PNG, WebP, or GIF image.")
                return redirect("workspaces:settings", workspace_id=workspace.id)

            # Validate file size (2 MB max)
            max_size = 2 * 1024 * 1024
            if icon.size > max_size:
                messages.error(request, "Logo must be under 2 MB.")
                return redirect("workspaces:settings", workspace_id=workspace.id)

            # Delete old icon before saving new one
            if workspace.icon:
                workspace.icon.delete(save=False)
            workspace.icon = icon

        workspace.save()
        messages.success(request, "Workspace settings updated.")
        return redirect("workspaces:settings", workspace_id=workspace.id)

    active_count = Workspace.objects.filter(organization=workspace.organization, is_archived=False).count()
    is_last_active = active_count <= 1 and not workspace.is_archived
    can_archive = is_owner_or_manager and not workspace.is_archived and not is_last_active
    can_delete = is_owner_or_manager and not is_last_active

    return render(
        request,
        "workspaces/settings.html",
        {
            "workspace": workspace,
            "settings_active": "general",
            "is_owner_or_manager": is_owner_or_manager,
            "can_archive": can_archive,
            "can_delete": can_delete,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def approvals_settings(request, workspace_id):
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise Http404 from None

    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if not membership:
        raise Http404

    is_owner_or_manager = membership.workspace_role in (
        WorkspaceMembership.WorkspaceRole.OWNER,
        WorkspaceMembership.WorkspaceRole.MANAGER,
    )

    if request.method == "POST":
        if not is_owner_or_manager:
            raise Http404

        mode = request.POST.get("approval_workflow_mode", "")
        valid_modes = Workspace.ApprovalWorkflowMode.values
        if mode not in valid_modes:
            messages.error(request, "Invalid approval workflow mode.")
            return redirect("workspaces:approvals_settings", workspace_id=workspace.id)

        # Only switching a workflow on needs the plan. Keeping the current
        # mode or turning approvals off always works, so a downgrade never
        # traps a workspace in a workflow it can't leave. Client sign-off
        # happens in the client portal, so that mode needs both features.
        required = []
        if mode != workspace.approval_workflow_mode and mode != Workspace.ApprovalWorkflowMode.NONE:
            required.append(Feature.APPROVALS)
            if mode == Workspace.ApprovalWorkflowMode.REQUIRED_INTERNAL_AND_CLIENT:
                required.append(Feature.CLIENT_PORTAL)
        if required:
            try:
                for feature in required:
                    require_feature(workspace.organization, feature)
            except PlanLimitError as exc:
                messages.error(request, exc.message)
                return redirect("workspaces:approvals_settings", workspace_id=workspace.id)

        workspace.approval_workflow_mode = mode
        workspace.save(update_fields=["approval_workflow_mode", "updated_at"])
        messages.success(request, "Approval workflow updated.")
        return redirect("workspaces:approvals_settings", workspace_id=workspace.id)

    return render(
        request,
        "workspaces/approvals_settings.html",
        {
            "workspace": workspace,
            "settings_active": "approvals",
            "is_owner_or_manager": is_owner_or_manager,
            "approval_modes": Workspace.ApprovalWorkflowMode,
        },
    )


# Bounds on the brand voice, which is sent with every AI prompt from this
# workspace — long enough for a real style guide, short enough to keep each
# request cheap.
BRAND_VOICE_FIELD_MAX = 1000
BRAND_VOICE_EXAMPLE_MAX = 2000
BRAND_VOICE_MAX_EXAMPLES = 3


REVIEW_POLICY_MAX = 1000


@login_required
@require_http_methods(["GET", "POST"])
def brand_voice_settings(request, workspace_id):
    """The workspace's Brand kit page: brand voice, logos, colours, fonts, copy rules and the review reply policy."""
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise Http404 from None

    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if not membership:
        raise Http404

    is_owner_or_manager = membership.workspace_role in (
        WorkspaceMembership.WorkspaceRole.OWNER,
        WorkspaceMembership.WorkspaceRole.MANAGER,
    )

    if request.method == "POST":
        if not is_owner_or_manager:
            raise Http404
        # A form without a section is the brand voice form, as it was before the kit.
        section = request.POST.get("section", "voice")
        if section == "voice":
            return _save_brand_voice(request, workspace)
        if section == "review":
            policy = request.POST.get("review_reply_policy", "").strip()
            if len(policy) > REVIEW_POLICY_MAX:
                messages.error(request, f"Keep the review reply policy under {REVIEW_POLICY_MAX:,} characters.")
            else:
                workspace.review_reply_policy = policy
                workspace.save(update_fields=["review_reply_policy", "updated_at"])
                messages.success(request, "Review reply policy updated.")
            return redirect("workspaces:brand_voice_settings", workspace_id=workspace.id)
        try:
            kit = brand_kit.kit_for(workspace=workspace)
            brand_kit.update(kit, brand_kit_forms.kit_data_from_post(request.POST, section))
        except brand_kit.BrandKitError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Brand kit updated.")
        return redirect("workspaces:brand_voice_settings", workspace_id=workspace.id)

    examples = list(workspace.brand_voice_examples or [])
    # Not created until something is saved, so just looking at the page writes nothing.
    kit = BrandKit.objects.filter(workspace=workspace).first() or BrandKit(workspace=workspace)
    return render(
        request,
        "workspaces/brand_voice_settings.html",
        {
            "workspace": workspace,
            "settings_active": "brand_voice",
            "is_owner_or_manager": is_owner_or_manager,
            # Always render three example boxes, filled in order.
            "example_slots": (examples + [""] * BRAND_VOICE_MAX_EXAMPLES)[:BRAND_VOICE_MAX_EXAMPLES],
            "field_max": BRAND_VOICE_FIELD_MAX,
            "example_max": BRAND_VOICE_EXAMPLE_MAX,
            "rules_max": brand_kit.RULES_MAX,
            "review_policy_max": REVIEW_POLICY_MAX,
            "inherited_from_org": BrandKit.objects.filter(organization_id=workspace.organization_id).exists(),
            **brand_kit_forms.page_context(kit),
        },
    )


def _save_brand_voice(request, workspace):
    fields = {
        "brand_voice_tone": request.POST.get("brand_voice_tone", "").strip(),
        "brand_voice_words_to_use": request.POST.get("brand_voice_words_to_use", "").strip(),
        "brand_voice_words_to_avoid": request.POST.get("brand_voice_words_to_avoid", "").strip(),
    }
    examples = [e.strip() for e in request.POST.getlist("brand_voice_examples") if e.strip()]
    if any(len(value) > BRAND_VOICE_FIELD_MAX for value in fields.values()):
        messages.error(request, f"Keep each brand voice field under {BRAND_VOICE_FIELD_MAX:,} characters.")
        return redirect("workspaces:brand_voice_settings", workspace_id=workspace.id)
    if len(examples) > BRAND_VOICE_MAX_EXAMPLES or any(len(e) > BRAND_VOICE_EXAMPLE_MAX for e in examples):
        messages.error(
            request,
            f"Add up to {BRAND_VOICE_MAX_EXAMPLES} example posts of at most {BRAND_VOICE_EXAMPLE_MAX:,} characters each.",
        )
        return redirect("workspaces:brand_voice_settings", workspace_id=workspace.id)

    for name, value in fields.items():
        setattr(workspace, name, value)
    workspace.brand_voice_examples = examples
    workspace.save(update_fields=[*fields, "brand_voice_examples", "updated_at"])
    messages.success(request, "Brand voice updated.")
    return redirect("workspaces:brand_voice_settings", workspace_id=workspace.id)


HASHTAG_SET_NAME_MAX = 100
SIGNATURE_NAME_MAX = 100
SIGNATURE_BODY_MAX = 1000


def _member_workspace(request, workspace_id):
    """The workspace, whether the user may manage its shared content, or 404 for a non-member."""
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise Http404 from None

    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if not membership:
        raise Http404

    can_manage = membership.workspace_role in (
        WorkspaceMembership.WorkspaceRole.OWNER,
        WorkspaceMembership.WorkspaceRole.MANAGER,
    )
    return workspace, can_manage


def _get_or_404(queryset, pk):
    try:
        return queryset.get(pk=pk)
    except (queryset.model.DoesNotExist, ValidationError, ValueError):
        raise Http404 from None


@login_required
@require_http_methods(["GET", "POST"])
def hashtag_sets_settings(request, workspace_id):
    workspace, can_manage = _member_workspace(request, workspace_id)
    sets = HashtagSet.objects.for_workspace(workspace.id)

    if request.method == "POST":
        if not can_manage:
            raise Http404

        action = request.POST.get("action")
        if action == "delete":
            _get_or_404(sets, request.POST.get("set_id")).delete()
            messages.success(request, "Hashtag set deleted.")
            return redirect("workspaces:hashtag_sets_settings", workspace_id=workspace.id)

        if action in ("create", "update"):
            hashtag_set = _get_or_404(sets, request.POST.get("set_id")) if action == "update" else None
            name = request.POST.get("name", "").strip()
            hashtags = parse_hashtags(request.POST.get("hashtags", ""))
            error = None
            if not name or len(name) > HASHTAG_SET_NAME_MAX:
                error = f"Give the set a name of up to {HASHTAG_SET_NAME_MAX} characters."
            elif not hashtags:
                error = "Add at least one hashtag."
            elif len(hashtags) > MAX_HASHTAGS_PER_SET:
                error = f"A set holds up to {MAX_HASHTAGS_PER_SET} hashtags."
            elif sets.filter(name__iexact=name).exclude(pk=hashtag_set.pk if hashtag_set else None).exists():
                error = "You already have a set with that name."
            if error:
                messages.error(request, error)
            elif hashtag_set:
                hashtag_set.name = name
                hashtag_set.hashtags = hashtags
                hashtag_set.save(update_fields=["name", "hashtags", "updated_at"])
                messages.success(request, "Hashtag set updated.")
            else:
                HashtagSet.objects.create(workspace=workspace, name=name, hashtags=hashtags, created_by=request.user)
                messages.success(request, "Hashtag set added.")
        return redirect("workspaces:hashtag_sets_settings", workspace_id=workspace.id)

    return render(
        request,
        "workspaces/hashtag_sets_settings.html",
        {
            "workspace": workspace,
            "settings_active": "hashtags",
            "can_manage": can_manage,
            "hashtag_sets": sets,
            "name_max": HASHTAG_SET_NAME_MAX,
            "max_hashtags": MAX_HASHTAGS_PER_SET,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def signatures_settings(request, workspace_id):
    workspace, can_manage = _member_workspace(request, workspace_id)
    signatures = Signature.objects.available(workspace.id, request.user)

    if request.method == "POST":
        action = request.POST.get("action")
        redirect_to = redirect("workspaces:signatures_settings", workspace_id=workspace.id)

        def editable(signature):
            """A personal signature is its owner's; a shared one needs a manager."""
            return can_manage or signature.user_id is not None

        if action == "delete":
            signature = _get_or_404(signatures, request.POST.get("signature_id"))
            if not editable(signature):
                raise Http404
            signature.delete()
            messages.success(request, "Signature deleted.")
            return redirect_to

        if action in ("create", "update"):
            if action == "update":
                signature = _get_or_404(signatures, request.POST.get("signature_id"))
                if not editable(signature):
                    raise Http404
                owner = {"workspace": signature.workspace, "user": signature.user}
            else:
                signature = None
                if request.POST.get("scope") == "workspace":
                    if not can_manage:
                        raise Http404
                    owner = {"workspace": workspace, "user": None}
                else:
                    owner = {"workspace": None, "user": request.user}

            name = request.POST.get("name", "").strip()
            body = request.POST.get("body", "").strip()
            is_default = request.POST.get("is_default") == "on"
            if not name or len(name) > SIGNATURE_NAME_MAX:
                messages.error(request, f"Give the signature a name of up to {SIGNATURE_NAME_MAX} characters.")
                return redirect_to
            if not body or len(body) > SIGNATURE_BODY_MAX:
                messages.error(request, f"Write a signature of up to {SIGNATURE_BODY_MAX:,} characters.")
                return redirect_to

            with transaction.atomic():
                if is_default:
                    # One default per owner: the new one replaces the old.
                    Signature.objects.filter(**owner, is_default=True).exclude(
                        pk=signature.pk if signature else None
                    ).update(is_default=False)
                if signature:
                    signature.name, signature.body, signature.is_default = name, body, is_default
                    signature.save(update_fields=["name", "body", "is_default", "updated_at"])
                    messages.success(request, "Signature updated.")
                else:
                    Signature.objects.create(**owner, name=name, body=body, is_default=is_default)
                    messages.success(request, "Signature added.")
        return redirect_to

    return render(
        request,
        "workspaces/signatures_settings.html",
        {
            "workspace": workspace,
            "settings_active": "signatures",
            "can_manage": can_manage,
            "workspace_signatures": [s for s in signatures if s.user_id is None],
            "personal_signatures": [s for s in signatures if s.user_id is not None],
            "name_max": SIGNATURE_NAME_MAX,
            "body_max": SIGNATURE_BODY_MAX,
        },
    )
