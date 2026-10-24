"""Forms for the account card's channel options."""

from django import forms


class ChannelOptionsForm(forms.Form):
    """Display-only overrides for a connected account: nickname, picture and profile address.

    None of these is sent to the platform. The two addresses end up in ``<img src>`` and ``<a href>``, so they
    must be ``https`` (``URLField`` alone also accepts ``http`` and ``ftp``).
    """

    nickname = forms.CharField(max_length=100, required=False, strip=True)
    custom_avatar_url = forms.URLField(max_length=2000, required=False, assume_scheme="https")
    custom_profile_url = forms.URLField(max_length=2000, required=False, assume_scheme="https")

    def _https(self, name: str) -> str:
        value = self.cleaned_data.get(name, "")
        if value and not value.lower().startswith("https://"):
            raise forms.ValidationError("Use an address that starts with https://.")
        return value

    def clean_custom_avatar_url(self):
        return self._https("custom_avatar_url")

    def clean_custom_profile_url(self):
        return self._https("custom_profile_url")
