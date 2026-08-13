"""Tests for the portal client helpers."""

from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.regioenergie_import.api import (
    extract_login_form,
    looks_like_html,
    parse_csv,
)

ZURICH = ZoneInfo("Europe/Zurich")

CSV = """Datum;Zeit;Wert
13.08.2026;00:15;0.125
13.08.2026;00:30;0.250
13.08.2026;00:45;
13.08.2026;01:00;0.375
"""


def test_parse_csv_reads_german_timestamps() -> None:
    """Values are parsed with the Swiss portal timezone."""
    readings = parse_csv(CSV)

    assert len(readings) == 4
    assert readings[0].timestamp == datetime(2026, 8, 13, 0, 15, tzinfo=ZURICH)
    assert readings[0].value == 0.125
    assert readings[-1].timestamp == datetime(2026, 8, 13, 1, 0, tzinfo=ZURICH)


def test_parse_csv_treats_empty_values_as_zero() -> None:
    """An empty third column counts as no consumption, as in the original."""
    assert parse_csv(CSV)[2].value == 0.0


def test_parse_csv_skips_header_and_broken_rows() -> None:
    """Header, short rows and unparsable values are ignored."""
    csv = (
        "Datum;Zeit;Wert\n"
        "13.08.2026;00:15\n"
        "13.08.2026;00:30;not-a-number\n"
        "not a csv line\n"
        "\n"
        "13.08.2026;00:45;1.5\n"
    )
    readings = parse_csv(csv)

    assert len(readings) == 1
    assert readings[0].value == 1.5


def test_parse_csv_accepts_crlf_and_seconds() -> None:
    """Windows line endings and a seconds component still parse."""
    readings = parse_csv("Datum;Zeit;Wert\r\n13.08.2026;00:15:00;2.0\r\n")

    assert len(readings) == 1
    assert readings[0].timestamp == datetime(2026, 8, 13, 0, 15, tzinfo=ZURICH)
    assert readings[0].value == 2.0


def test_extract_login_form_finds_fields_and_hidden_values() -> None:
    """The form with a password field wins, hidden fields are carried over."""
    html = """
    <html><body>
      <form action="/de/suche.php"><input type="text" name="q"></form>
      <form action="/de/login.php" method="post">
        <input type="hidden" name="csrf_token" value="abc123">
        <input type="text" name="benutzer">
        <input type="password" name="passwort">
        <input type="checkbox" name="remember" value="1">
        <input type="submit" name="login" value="Anmelden">
      </form>
    </body></html>
    """
    form = extract_login_form(html)

    assert form is not None
    assert form.action == "/de/login.php"
    assert form.username_field == "benutzer"
    assert form.password_field == "passwort"
    assert form.fields == {"csrf_token": "abc123"}
    # An unchecked checkbox is not submitted by a browser either.
    assert "remember" not in form.fields


def test_extract_login_form_leaves_the_password_reset_button_alone() -> None:
    """The real okp.regioenergie.ch markup must not trigger a password reset.

    Login and password reset share one form there. The login button carries no
    name, while the reset button is `name="login"` - posting it along with the
    credentials would reset the password on every poll.
    """
    html = """
    <form class="login-form" name="login" method="post" action="/de/services/login.php">
      <input class="form-control" name="email" placeholder="E-Mail-Adresse" type="text">
      <input class="form-control" name="passwort" placeholder="Passwort" type="password">
      <button type="submit" class="btn btn-default fw">Login</button>
      <div id="div_pw_vergessen" style="display:none">
        <input class="form-control" type="text" name="pw_vergessen" id="pw_vergessen">
        <input type="submit" name="login" value="Passwort zur&uuml;cksetzen">
      </div>
    </form>
    """
    form = extract_login_form(html)

    assert form is not None
    assert form.username_field == "email"
    assert form.password_field == "passwort"
    assert "login" not in form.fields
    assert form.fields == {"pw_vergessen": ""}


def test_extract_login_form_ignores_pages_without_password() -> None:
    """A page with only a search form is not a login page."""
    assert extract_login_form("<form><input type='text' name='q'></form>") is None


def test_extract_login_form_survives_unclosed_form() -> None:
    """Sloppy markup still yields the login form."""
    html = "<form action='/x'><input name='u'><input type='password' name='p'>"

    form = extract_login_form(html)

    assert form is not None
    assert form.password_field == "p"


def test_looks_like_html_separates_pages_from_csv() -> None:
    """The CSV export is told apart from an error or login page."""
    assert looks_like_html("<!DOCTYPE html><html>...")
    assert looks_like_html("\n  <html>")
    assert not looks_like_html("Datum;Zeit;Wert\n13.08.2026;00:15;1.0")
