import json
import secrets

from django import template
from django.template.loader import render_to_string
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def signup_mode():
    """The configured SIGNUP_MODE, so the closed-signup notice can say why."""
    from django.conf import settings

    return settings.SIGNUP_MODE


@register.simple_tag(takes_context=True)
def invite_lapsed(context):
    """Whether the visitor's invitation expired or was used since they followed it."""
    from apps.accounts.signup_links import has_lapsed_invite

    return has_lapsed_invite(context.get("request"))


@register.filter(is_safe=True)
def json_attr(value):
    """Serialize a Python value as a JSON literal safe to embed inside an
    HTML attribute (e.g. Alpine.js x-data).

    json.dumps produces JSON, then HTML-escape covers &, <, >, ", '. The
    browser HTML-decodes the attribute value before the JS engine parses
    it, so Alpine still sees valid JSON.

    Pass Python values (list/dict/None), NOT pre-serialized JSON strings.
    None and empty-string (Django's `string_if_invalid` fallback when a
    template variable like `post.tags` doesn't resolve) both become `[]`,
    matching the prior `|default:'[]'|safe` idiom.
    """
    if value is None or value == "":
        return mark_safe(escape("[]"))
    return mark_safe(escape(json.dumps(value, ensure_ascii=False, default=str)))


@register.inclusion_tag("components/ui_select.html")
def ui_select(
    *,
    model,
    options,
    multiple=False,
    onchange="",
    placeholder="Select",
    value_field="id",
    label_field="",
    icon_field="",
    icon="",
):
    """A styled single/multi select dropdown (Alpine + checkbox/click list).

    A drop-in upgrade for a plain ``<select>`` in an Alpine/HTMX toolbar. The
    panel is ``position: fixed`` (anchored on open) so an ``overflow`` filter
    row can't clip it. Bind it to a property in the enclosing ``x-data`` scope:
    an **array** when ``multiple`` (empty = "all"), otherwise a **string**.

    Params:
      model        Alpine expression holding the selection, e.g. "filters.status".
      options      iterable of model instances, or ``{"value","label","icon"}`` dicts.
      multiple     checkbox multi-select (True) vs single-select (False).
      onchange     Alpine expression run after a change, e.g. "reloadTab()".
      placeholder  trigger label shown when nothing is selected.
      value_field / label_field / icon_field
                   attribute names read off model instances (ignored for dicts).
                   ``icon_field`` is read as a platform code and rendered as a
                   per-option badge.
      icon         leading glyph for the trigger itself — one of
                   status / channel / tag / clock (see components/_filter_icon.html).
                   Omit for no icon.
    """
    norm = []
    for o in options:
        if isinstance(o, dict):
            value, label, opt_icon = o.get("value"), o.get("label"), o.get("icon")
        elif isinstance(o, (tuple, list)) and len(o) >= 2:
            # (value, label) pairs, e.g. Django `choices`.
            value, label, opt_icon = o[0], o[1], None
        elif isinstance(o, str):
            value = label = o
            opt_icon = None
        else:
            value = getattr(o, value_field, None)
            label = getattr(o, label_field) if label_field else str(o)
            opt_icon = getattr(o, icon_field, None) if icon_field else None
        norm.append({"value": str(value) if value is not None else "", "label": label, "icon": opt_icon})

    return {
        "model": model,
        "options": norm,
        # value+label only, for the Alpine trigger-label lookup in single mode.
        "options_js": [{"value": o["value"], "label": str(o["label"])} for o in norm],
        "multiple": bool(multiple),
        "onchange": onchange,
        "placeholder": placeholder,
        "icon": icon,
    }


@register.inclusion_tag("components/icon_button.html")
def icon_button(
    *,
    label,
    icon,
    click="",
    expanded="",
    controls="",
    href="",
    size="md",
    variant="ghost",
    extra_class="",
    type="button",
    icon_size=20,
    id="",
):
    """An icon-only button or link with a required accessible name.

    Usage and arguments are documented at the top of components/icon_button.html.
    An empty ``label`` is a template error: icon-only controls must be named.
    """
    if not str(label).strip():
        raise template.TemplateSyntaxError(
            "icon_button requires a non-empty label (it is the control's accessible name)"
        )
    return {
        "label": label,
        "icon": icon,
        "click": click,
        "expanded": expanded,
        "controls": controls,
        "href": href,
        "size": size,
        "variant": variant,
        "extra_class": extra_class,
        "type": type,
        "icon_size": icon_size,
        "id": id,
    }


@register.simple_block_tag
def modal(content, *, open, title, description="", size="md", teleport=True, return_focus=""):
    """An accessible modal dialog wrapping the block content.

    Usage and arguments are documented at the top of components/modal.html.
    ``open`` is an assignable Alpine expression naming the boolean that shows the dialog.
    """
    if not str(title).strip():
        raise template.TemplateSyntaxError("modal requires a non-empty title (it labels the dialog)")
    return render_to_string(
        "components/modal.html",
        {
            "open": open,
            "close_expr": f"{open} = false",
            "title": title,
            "description": description,
            "size": size,
            "teleport": teleport,
            "return_focus": return_focus,
            "uid": f"pd-modal-{secrets.token_hex(4)}",
            "content": content,
        },
    )
