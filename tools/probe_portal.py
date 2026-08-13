#!/usr/bin/env python3
"""Probe a utility portal for its login form and its load profile export.

Standalone helper, standard library only - run it on any machine that can
reach the portal to find out how the login actually works and whether a meter
number returns data:

    python3 tools/probe_portal.py \
        --username YOU --password SECRET \
        --meter CH1007201234500000000000000014185

It prints the discovered login form (including hidden/CSRF fields), whether the
login was accepted, and the first lines of the CSV export. The field names it
reports are exactly what the integration expects in its advanced login options.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from html.parser import HTMLParser

DEFAULT_BASE_URL = "https://okp.regioenergie.ch"
EXPORT_PATH = "/de/services/lastgang-export.php"
LOGIN_PATH_CANDIDATES = (
    "/de/services/login.php",
    "/de/login.php",
    "/de/anmelden.php",
    "/login.php",
    "/de/",
    "/",
)
USER_AGENT = "Mozilla/5.0 (compatible; regioenergie-import-probe/1.0)"
USERNAME_INPUT_TYPES = ("text", "email", "tel", "")
SUBMIT_INPUT_TYPES = ("submit", "reset", "image", "button")


class FormCollector(HTMLParser):
    """Collect every form on a page together with its input elements."""

    def __init__(self) -> None:
        """Initialise the collector."""
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag, attrs):
        """Start a new form, or record one of its inputs."""
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag == "form":
            self._flush()
            self._current = {"action": attributes.get("action"), "inputs": []}
        elif tag in ("input", "button", "select", "textarea") and self._current:
            attributes.setdefault("tag", tag)
            self._current["inputs"].append(attributes)

    def handle_endtag(self, tag):
        """Close the form that is being collected."""
        if tag == "form":
            self._flush()

    def close(self):
        """Finish parsing and keep a trailing unclosed form."""
        super().close()
        self._flush()

    def _flush(self):
        if self._current:
            self.forms.append(self._current)
            self._current = None


def extract_login_form(html: str) -> dict | None:
    """Return the first form on the page that asks for a password."""
    collector = FormCollector()
    collector.feed(html)
    collector.close()

    for form in collector.forms:
        result = {
            "action": form["action"],
            "username_field": None,
            "password_field": None,
            "fields": {},
        }
        for attributes in form["inputs"]:
            name = attributes.get("name")
            if not name:
                continue
            input_type = attributes.get("type", "").lower()
            if attributes.get("tag") == "button" and not input_type:
                input_type = "submit"

            if input_type == "password":
                if result["password_field"] is None:
                    result["password_field"] = name
                continue
            if input_type in USERNAME_INPUT_TYPES and result["password_field"] is None:
                result["username_field"] = name
                continue
            if input_type in SUBMIT_INPUT_TYPES:
                # Only the clicked control is submitted by a browser - and the
                # password reset button shares the form with the login.
                continue
            if input_type in ("checkbox", "radio") and "checked" not in attributes:
                continue
            result["fields"][name] = attributes.get("value", "")

        if result["password_field"]:
            return result
    return None


def request(opener, url: str, data: dict | None = None) -> tuple[str, str]:
    """Perform a GET or POST and return the body and the final URL."""
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=60) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace"), response.geturl()


def main() -> int:
    """Discover the login form, sign in and fetch a sample export."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password")
    parser.add_argument("--login-url", help="skip discovery and use this page")
    parser.add_argument("--username-field")
    parser.add_argument("--password-field")
    parser.add_argument("--meter", help="meter number to test the CSV export with")
    parser.add_argument("--obis", default="1-1:1.29.0")
    parser.add_argument("--type", dest="export_type", default="WirkBezug")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    password = args.password or getpass.getpass("Portal password: ")
    base_url = args.base_url.rstrip("/")

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    candidates = (
        [args.login_url]
        if args.login_url
        else [base_url + path for path in LOGIN_PATH_CANDIDATES]
    )

    form = None
    page_url = ""
    for candidate in candidates:
        print(f"* Looking for a login form on {candidate}")
        try:
            body, page_url = request(opener, candidate)
        except (urllib.error.URLError, OSError) as err:
            print(f"  unreachable: {err}")
            continue
        form = extract_login_form(body)
        if form:
            print(f"  found a login form on {page_url}")
            break
        print("  no password field on this page")

    if not form:
        print(
            "\nNo login form found. Open the portal in a browser, look at the "
            "login form in the developer tools and pass --login-url, "
            "--username-field and --password-field."
        )
        return 1

    username_field = args.username_field or form["username_field"]
    password_field = args.password_field or form["password_field"]
    action = urllib.parse.urljoin(page_url, form["action"] or page_url)

    print("\n--- discovered login form ---")
    print(f"  action:         {action}")
    print(f"  username field: {username_field}")
    print(f"  password field: {password_field}")
    print(f"  extra fields:   {form['fields'] or '(none)'}")

    if not username_field or not password_field:
        print("\nCould not tell which field is which - pass them explicitly.")
        return 1

    payload = dict(form["fields"])
    payload[username_field] = args.username
    payload[password_field] = password

    print("\n* Submitting the login")
    try:
        body, final_url = request(opener, action, payload)
    except (urllib.error.URLError, OSError) as err:
        print(f"  login request failed: {err}")
        return 1

    if extract_login_form(body):
        print(f"  the answer from {final_url} still contains a login form")
        print("  -> credentials rejected, or the form needs another field")
        return 1
    print(f"  login accepted, landed on {final_url}")

    if not args.meter:
        print("\nPass --meter to also test the CSV export.")
        return 0

    end = date.today()
    start = end - timedelta(days=args.days)
    query = urllib.parse.urlencode(
        {
            "zaehler": args.meter,
            "obis": args.obis,
            "datumVon": start.isoformat(),
            "datumBis": end.isoformat(),
            "type": args.export_type,
        },
        safe=":.-",
    )
    export_url = f"{base_url}{EXPORT_PATH}?{query}"

    print(f"\n* Fetching {export_url}")
    try:
        body, _ = request(opener, export_url)
    except (urllib.error.URLError, OSError) as err:
        print(f"  export request failed: {err}")
        return 1

    if body.lstrip()[:200].lower().startswith(("<!doctype", "<html", "<")):
        print("  the export answered with a web page, not a CSV:")
        print("  " + body.strip()[:400].replace("\n", "\n  "))
        return 1

    lines = body.strip().splitlines()
    print(f"  got {len(lines)} lines, first five:")
    for line in lines[:5]:
        print(f"    {line}")

    rows = []
    for line in lines:
        parts = line.split(";")
        if len(parts) < 3 or "Datum" in line:
            continue
        rows.append((f"{parts[0].strip()} {parts[1].strip()}", parts[2].strip()))

    if rows:
        filled = [stamp for stamp, value in rows if value]
        print("\n--- data summary ---")
        print(f"  rows          : {len(rows)}")
        print(f"  range         : {rows[0][0]} .. {rows[-1][0]}")
        print(f"  empty values  : {len(rows) - len(filled)}")
        print(f"  last measured : {filled[-1] if filled else '(none)'}")
        print(
            "\n  Empty rows at the end are intervals the utility has not "
            "published yet;\n  the integration skips them instead of "
            "importing them as zero."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
