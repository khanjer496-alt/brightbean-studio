"""Render-only regression coverage: no database or external authentication calls."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from django import forms
from django.template import Context, Engine


class AuthForm(forms.Form):
    email = forms.EmailField(help_text="Use your workspace email.")
    password = forms.CharField(widget=forms.PasswordInput)


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def render_auth(name, form):
    templates = Path(__file__).resolve().parents[3] / "templates"
    # Replace only the page shell. Exercise the actual auth template and partials.
    engine = Engine(
        dirs=[str(templates)],
        loaders=[
            ("django.template.loaders.locmem.Loader", {"base.html": "{% block auth_content %}{% endblock %}"}),
            "django.template.loaders.filesystem.Loader",
        ],
        libraries={
            "static": "django.templatetags.static",
            "i18n": "django.templatetags.i18n",
            "allauth": "allauth.templatetags.allauth",
            "account": "allauth.account.templatetags.account",
            "socialaccount": "allauth.socialaccount.templatetags.socialaccount",
        },
    )
    return engine.get_template(f"account/{name}.html").render(
        Context(
            {
                "form": form,
                "brand_name": "PostDelegate",
                "brand_logo_static": "img/postdelegate-logo.svg",
                "signup_url": "/accounts/signup/",
                "login_url": "/accounts/login/",
            }
        )
    )


@pytest.mark.parametrize("name", ["login", "signup", "password_reset", "password_reset_from_key"])
def test_auth_page_displays_non_field_errors_and_associates_field_errors(name):
    form = AuthForm({"email": "not-an-email", "password": "never-echo-this-password"})
    if name == "password_reset":
        del form.fields["password"]
    assert not form.is_valid()
    form.add_error(None, "The request could not be completed. Please try again.")
    html = render_auth(name, form)
    elements = Elements(html).elements
    assert "The request could not be completed. Please try again." in html
    assert any(attrs.get("role") == "alert" for _, attrs in elements)
    email = next(attrs for tag, attrs in elements if tag == "input" and attrs.get("name") == "email")
    assert email["aria-invalid"] == "true"
    for described_id in email["aria-describedby"].split():
        assert any(attrs.get("id") == described_id for _, attrs in elements)
    assert "never-echo-this-password" not in html


def test_fields_element_renders_bound_fields():
    """allauth's stock pages (2FA code, reauthenticate, third-party signup) render forms through it."""
    templates = Path(__file__).resolve().parents[3] / "templates"
    page = "{% load allauth %}{% element fields form=form unlabeled=True %}{% endelement %}"
    engine = Engine(
        dirs=[str(templates)],
        loaders=[
            ("django.template.loaders.locmem.Loader", {"page.html": page}),
            "django.template.loaders.filesystem.Loader",
        ],
        libraries={"allauth": "allauth.templatetags.allauth"},
    )
    form = AuthForm({"email": "not-an-email", "password": "never-echo-this-password"})
    assert not form.is_valid()

    html = engine.get_template("page.html").render(Context({"form": form}))
    elements = Elements(html).elements

    inputs = {attrs["name"]: attrs for tag, attrs in elements if tag == "input"}
    assert inputs["email"]["id"] == "id_email"
    assert inputs["email"]["type"] == "email"
    assert inputs["email"]["aria-invalid"] == "true"
    assert any(attrs.get("id") == inputs["email"]["aria-describedby"] for _, attrs in elements)
    assert inputs["password"]["type"] == "password"
    labels = {attrs.get("for"): attrs for tag, attrs in elements if tag == "label"}
    assert {"id_email", "id_password"} <= labels.keys()
    assert "sr-only" in labels["id_email"]["class"]
    assert "never-echo-this-password" not in html
