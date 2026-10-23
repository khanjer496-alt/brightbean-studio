"""The report definition form: one place that both the page and its live preview validate with."""

from __future__ import annotations

from django import forms

from apps.social_accounts.models import SocialAccount

from .models import SECTIONS, ReportDefinition

INPUT = "form-input-styled text-sm"


class ReportDefinitionForm(forms.ModelForm):
    social_accounts = forms.ModelMultipleChoiceField(
        queryset=SocialAccount.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Accounts",
        help_text="Leave all unticked to include every connected account.",
    )
    sections = forms.MultipleChoiceField(
        choices=SECTIONS,
        widget=forms.CheckboxSelectMultiple,
        label="Sections",
        error_messages={"required": "Choose at least one section."},
    )
    recipients = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": INPUT, "autocomplete": "off"}),
        label="Recipients",
        help_text="One email address per line (or separated by commas).",
    )

    class Meta:
        model = ReportDefinition
        fields = [
            "name",
            "social_accounts",
            "range_type",
            "custom_start",
            "custom_end",
            "sections",
            "compare",
            "accent_color",
            "white_label",
            "recipients",
            "visible_to_clients",
            "frequency",
            "weekday",
            "day_of_month",
            "send_time",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": INPUT, "maxlength": 120, "autocomplete": "off"}),
            "range_type": forms.Select(attrs={"class": INPUT}),
            "custom_start": forms.DateInput(attrs={"type": "date", "class": INPUT}),
            "custom_end": forms.DateInput(attrs={"type": "date", "class": INPUT}),
            "accent_color": forms.TextInput(
                attrs={
                    "type": "color",
                    "class": "h-11 w-16 cursor-pointer rounded-lg border border-line bg-surface-0 p-1",
                }
            ),
            "frequency": forms.Select(attrs={"class": INPUT}),
            "weekday": forms.Select(
                attrs={"class": INPUT},
                choices=[
                    (i, d)
                    for i, d in enumerate(
                        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
                    )
                ],
            ),
            "day_of_month": forms.NumberInput(attrs={"class": INPUT, "min": 1, "max": 28}),
            "send_time": forms.TimeInput(attrs={"type": "time", "class": INPUT}, format="%H:%M"),
        }
        labels = {
            "name": "Report name",
            "range_type": "Date range",
            "custom_start": "From",
            "custom_end": "To",
            "compare": "Compare with the previous period",
            "accent_color": "Accent colour",
            "white_label": "White label (hide product branding)",
            "visible_to_clients": "Show in the client portal",
            "frequency": "Send",
            "weekday": "On",
            "day_of_month": "On day of the month",
            "send_time": "At",
            "is_active": "Schedule is on",
        }

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        self.instance.workspace = workspace
        self.fields["social_accounts"].queryset = SocialAccount.objects.filter(
            workspace=workspace, connection_status=SocialAccount.ConnectionStatus.CONNECTED
        ).order_by("platform", "account_name")
        self.fields["accent_color"].required = False
        if self.instance.pk and not self.is_bound:
            self.initial["recipients"] = "\n".join(self.instance.recipients)
        if not self.instance.accent_color:
            self.initial.setdefault("accent_color", workspace.primary_color or "#0b6bcb")

    def clean_recipients(self):
        raw = self.cleaned_data.get("recipients", "")
        return [part.strip() for line in raw.splitlines() for part in line.split(",") if part.strip()]

    def clean_sections(self):
        chosen = set(self.cleaned_data["sections"])
        return [key for key, _ in SECTIONS if key in chosen]

    def clean_accent_color(self):
        return (self.cleaned_data.get("accent_color") or "").lower()
