from django import forms


class ImpersonationForm(forms.Form):
    reason = forms.CharField(
        max_length=200,
        label="Why do you need to look?",
        help_text="A ticket reference or a short note. It is kept in the audit log.",
    )
