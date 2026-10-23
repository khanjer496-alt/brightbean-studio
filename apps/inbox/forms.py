"""Forms for the Unified Social Inbox."""

import uuid

from django import forms

from .models import InboxMessage, InboxSLAConfig, SavedReply


class ReplyForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "Write a reply...",
                "class": "form-input w-full",
            }
        ),
    )


class InternalNoteForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": "Add an internal note...",
                "class": "form-input w-full",
            }
        ),
    )


class AssignForm(forms.Form):
    assigned_to = forms.UUIDField(required=False)


class StatusForm(forms.Form):
    status = forms.ChoiceField(choices=InboxMessage.Status.choices)


class SentimentForm(forms.Form):
    sentiment = forms.ChoiceField(choices=InboxMessage.Sentiment.choices)


class TriageForm(forms.Form):
    """One keyboard-triage action on a single message row."""

    action = forms.ChoiceField(
        choices=[("resolve", "Resolve"), ("unread", "Mark unread"), ("assign_me", "Assign to me")]
    )


class BulkActionForm(forms.Form):
    ACTIONS_WITH_VALUE = ("assign", "status", "sentiment", "note")

    message_ids = forms.CharField(required=False)
    action = forms.ChoiceField(
        choices=[
            ("mark_read", "Mark as Read"),
            ("mark_unread", "Mark as Unread"),
            ("resolve", "Resolve"),
            ("archive", "Archive"),
            ("assign", "Assign"),
            ("unassign", "Unassign"),
            ("status", "Change status"),
            ("sentiment", "Change sentiment"),
            ("note", "Add note"),
        ]
    )
    value = forms.CharField(required=False, max_length=5000)
    # "Select all matching the filter": the server re-runs the filter, and only
    # acts if it still matches as many messages as the person confirmed.
    select_all = forms.BooleanField(required=False)
    # The feed's filter query string; the list is re-rendered under it.
    filters = forms.CharField(required=False, max_length=2000)
    expected_count = forms.IntegerField(required=False, min_value=0)

    def clean_message_ids(self):
        raw = self.cleaned_data["message_ids"]
        ids = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(uuid.UUID(part))
            except ValueError as exc:
                raise forms.ValidationError("Invalid message id.") from exc
        return ids

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        if data["select_all"]:
            if data.get("expected_count") is None:
                raise forms.ValidationError("Confirm how many messages match.")
        elif not data["message_ids"]:
            raise forms.ValidationError("Select at least one message.")
        action = data["action"]
        value = data.get("value", "").strip()
        if action in self.ACTIONS_WITH_VALUE and not value:
            raise forms.ValidationError("Choose a value for this action.")
        if action == "status" and value not in InboxMessage.Status.values:
            raise forms.ValidationError("Invalid status.")
        if action == "sentiment" and value not in InboxMessage.Sentiment.values:
            raise forms.ValidationError("Invalid sentiment.")
        if action == "assign":
            try:
                uuid.UUID(value)
            except ValueError as exc:
                raise forms.ValidationError("Invalid assignee.") from exc
        data["value"] = value
        return data


class SavedReplyForm(forms.ModelForm):
    # Which star band of reviews offers this reply first; "Any message" keeps it for everything.
    class Meta:
        model = SavedReply
        fields = ["title", "body", "rating_band"]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "form-input w-full",
                    "placeholder": "Reply title",
                }
            ),
            "body": forms.Textarea(
                attrs={
                    "class": "form-input w-full",
                    "rows": 4,
                    "placeholder": "Reply body. Use {sender_name}, {account_name}, {post_url} for variables.",
                }
            ),
            "rating_band": forms.Select(attrs={"class": "form-input w-full"}),
        }


class SLAConfigForm(forms.ModelForm):
    class Meta:
        model = InboxSLAConfig
        fields = ["target_response_minutes", "is_active", "auto_resolve_on_reply"]
        widgets = {
            "target_response_minutes": forms.NumberInput(
                attrs={
                    "class": "form-input w-full",
                    "min": 1,
                }
            ),
        }
