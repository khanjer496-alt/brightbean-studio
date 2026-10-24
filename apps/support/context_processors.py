from .services import announcements_for


def support_context(request):
    """The "viewing as" banner, or the announcements this user hasn't closed."""
    impersonator = getattr(request, "impersonator", None)
    if impersonator is not None:
        return {"impersonation": {"staff": impersonator, "target": request.user}}
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"announcements": announcements_for(user, getattr(request, "org", None))}
