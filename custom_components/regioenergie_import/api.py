"""HTTP client for the Regio Energie customer portal.

The portal has no documented API. Data is read from the same CSV export the
web UI uses, behind a plain form login. Because the exact shape of the login
form is not published (and may differ between the utilities using this portal
software), the form is discovered at runtime: the login page is parsed, hidden
fields such as CSRF tokens are carried over, and the field names for user and
password are taken from the form itself. All of that can be overridden from
the integration options if discovery picks the wrong form.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

import aiohttp
from yarl import URL

from .const import EXPORT_PATH, LOGIN_PATH_CANDIDATES, PORTAL_TIMEZONE

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

_TIME_FORMATS = ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S")

_USERNAME_INPUT_TYPES = ("text", "email", "tel", "")

_SUBMIT_INPUT_TYPES = ("submit", "reset", "image", "button")


class RegioEnergieError(Exception):
    """Base class for all errors raised by the client."""


class RegioEnergieConnectionError(RegioEnergieError):
    """The portal could not be reached."""


class RegioEnergieAuthError(RegioEnergieError):
    """The portal rejected the credentials, or no login form was found."""


@dataclass(slots=True)
class MeterReading:
    """A single value of the load profile export, at the interval *end*.

    The timestamp is in UTC: Swiss local time repeats an hour every autumn, so
    only UTC keeps every quarter hour apart.
    """

    timestamp: datetime
    value: float
    #: True when the export had no value yet for this interval. The portal
    #: pads its answer up to the end of the requested range, so the tail of a
    #: fresh export is empty rather than zero.
    is_empty: bool = False


@dataclass
class LoginForm:
    """A login form discovered on a portal page."""

    action: str | None = None
    username_field: str | None = None
    password_field: str | None = None
    fields: dict[str, str] = field(default_factory=dict)


class _FormCollector(HTMLParser):
    """Collect every form on a page together with its input elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag == "form":
            # Nested forms are invalid HTML; treat a new <form> as a restart.
            self._flush()
            self._current = {"action": attributes.get("action"), "inputs": []}
        elif tag in ("input", "button", "select", "textarea") and self._current:
            attributes.setdefault("tag", tag)
            self._current["inputs"].append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._flush()

    def close(self) -> None:
        super().close()
        # Pages with an unclosed <form> would otherwise lose their last form.
        self._flush()

    def _flush(self) -> None:
        if self._current:
            self.forms.append(self._current)
            self._current = None


def extract_login_form(html: str) -> LoginForm | None:
    """Return the first form on the page that asks for a password."""
    collector = _FormCollector()
    collector.feed(html)
    collector.close()

    for form in collector.forms:
        result = LoginForm(action=form["action"])
        for attributes in form["inputs"]:
            name = attributes.get("name")
            if not name:
                continue
            input_type = attributes.get("type", "").lower()
            if attributes.get("tag") == "button" and not input_type:
                input_type = "submit"

            if input_type == "password":
                if result.password_field is None:
                    result.password_field = name
                continue
            # The user field is the last plain input ahead of the password one.
            if input_type in _USERNAME_INPUT_TYPES and result.password_field is None:
                result.username_field = name
                continue
            if input_type in _SUBMIT_INPUT_TYPES:
                # A browser only submits the control that was actually clicked.
                # Sending them all is not just noise: on okp.regioenergie.ch the
                # password reset button sits inside the very same form as the
                # login, so posting it would reset the password on every poll.
                continue
            if input_type in ("checkbox", "radio") and "checked" not in attributes:
                continue
            # Hidden fields (CSRF tokens, form ids) and the remaining inputs of
            # the form are posted back the way a browser would.
            result.fields[name] = attributes.get("value", "")

        if result.password_field:
            return result

    return None


def looks_like_html(text: str) -> bool:
    """Return True if the body is a web page rather than a CSV export."""
    return text.lstrip().startswith("<")


def _is_ambiguous(naive: datetime, tzinfo: ZoneInfo) -> bool:
    """Return True for a wall time that occurs twice (autumn DST change)."""
    return (
        naive.replace(tzinfo=tzinfo, fold=0).utcoffset()
        != naive.replace(tzinfo=tzinfo, fold=1).utcoffset()
    )


def parse_csv(text: str, timezone: str = PORTAL_TIMEZONE) -> list[MeterReading]:
    """Parse a load profile CSV export.

    Format: `;` separated, `Datum;Zeit;Wert`, German date format, one header
    line containing "Datum". Ported from the original cron script, including
    its tolerance for empty and malformed rows.

    The timestamps are Swiss local time. When the clocks go back in autumn the
    export runs through the hour after 02:00 twice; the second pass is
    recognised by the time jumping backwards and gets `fold=1`, so both passes
    keep their own UTC time.
    """
    tzinfo = ZoneInfo(timezone)
    readings: list[MeterReading] = []
    previous: datetime | None = None
    fold = 0

    for line in text.strip().splitlines():
        parts = line.split(";")
        if len(parts) < 3 or "Datum" in line:
            continue

        raw_value = parts[2].strip()
        is_empty = not raw_value
        if is_empty:
            value = 0.0
        else:
            try:
                value = float(raw_value)
            except ValueError:
                try:
                    value = float(raw_value.replace("'", "").replace(",", "."))
                except ValueError:
                    continue

        stamp = f"{parts[0].strip()} {parts[1].strip()}"
        for time_format in _TIME_FORMATS:
            try:
                naive = datetime.strptime(stamp, time_format)
            except ValueError:
                continue
            if not _is_ambiguous(naive, tzinfo):
                fold = 0
            elif previous is not None and naive < previous:
                fold = 1
            previous = naive
            readings.append(
                MeterReading(
                    naive.replace(tzinfo=tzinfo, fold=fold).astimezone(UTC),
                    value,
                    is_empty,
                )
            )
            break

    return readings


class RegioEnergieClient:
    """Thin client around the portal's form login and CSV export."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        username: str,
        password: str,
        *,
        login_url: str | None = None,
        username_field: str | None = None,
        password_field: str | None = None,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._login_url = login_url or None
        self._username_field = username_field or None
        self._password_field = password_field or None
        self._logged_in = False

    @property
    def base_url(self) -> str:
        """Return the normalised portal base URL."""
        return self._base_url

    async def async_login(self) -> None:
        """Authenticate against the portal.

        Raises RegioEnergieAuthError if no login form can be found or the
        portal answers the login POST with another login form.
        """
        self._logged_in = False
        candidates = (
            [self._login_url]
            if self._login_url
            else [self._base_url + path for path in LOGIN_PATH_CANDIDATES]
        )

        last_error: Exception | None = None
        for candidate in candidates:
            try:
                body, final_url = await self._async_get(candidate)
            except RegioEnergieConnectionError as err:
                last_error = err
                continue

            form = extract_login_form(body)
            if form is None:
                _LOGGER.debug("No login form found on %s", candidate)
                continue

            await self._async_submit_login(form, final_url)
            return

        # Explicit URL plus explicit field names: post blind. Portals that build
        # their form in JavaScript never expose one to the parser.
        if self._login_url and self._username_field and self._password_field:
            _LOGGER.debug("Posting credentials directly to %s", self._login_url)
            await self._async_submit_login(
                LoginForm(
                    action=self._login_url,
                    username_field=self._username_field,
                    password_field=self._password_field,
                ),
                self._login_url,
            )
            return

        if last_error is not None:
            raise RegioEnergieConnectionError(
                f"Portal at {self._base_url} could not be reached"
            ) from last_error
        raise RegioEnergieAuthError(
            "No login form found - set the login URL and field names in the "
            "integration options"
        )

    async def async_fetch_readings(
        self,
        meter_number: str,
        obis_code: str,
        export_type: str,
        start: date,
        end: date,
    ) -> list[MeterReading]:
        """Return the load profile readings for one meter."""
        return parse_csv(
            await self._async_fetch_csv(
                meter_number, obis_code, export_type, start, end
            )
        )

    async def _async_fetch_csv(
        self,
        meter_number: str,
        obis_code: str,
        export_type: str,
        start: date,
        end: date,
    ) -> str:
        """Fetch the raw CSV export, logging in again if the session expired."""
        if not self._logged_in:
            await self.async_login()

        body = await self._async_get_export(
            meter_number, obis_code, export_type, start, end
        )
        if not looks_like_html(body):
            return body

        _LOGGER.debug("Export answered with a web page, authenticating again")
        await self.async_login()
        body = await self._async_get_export(
            meter_number, obis_code, export_type, start, end
        )
        if looks_like_html(body):
            raise RegioEnergieAuthError(
                "Portal returned a web page instead of the CSV export - the "
                "credentials or the meter number may be wrong"
            )
        return body

    async def _async_get_export(
        self,
        meter_number: str,
        obis_code: str,
        export_type: str,
        start: date,
        end: date,
    ) -> str:
        """Request the CSV export for a single meter and date range."""
        query = urlencode(
            {
                "zaehler": meter_number,
                "obis": obis_code,
                "datumVon": start.strftime("%Y-%m-%d"),
                "datumBis": end.strftime("%Y-%m-%d"),
                "type": export_type,
            },
            # The portal is fed the OBIS code with its colon unencoded, exactly
            # as the browser and the original script do.
            safe=":.-",
        )
        body, _ = await self._async_get(f"{self._base_url}{EXPORT_PATH}?{query}")
        return body

    async def _async_submit_login(self, form: LoginForm, page_url: str) -> None:
        """Post the credentials of a discovered login form."""
        action = urljoin(page_url, form.action) if form.action else page_url
        payload = dict(form.fields)
        username_field = self._username_field or form.username_field
        password_field = self._password_field or form.password_field

        if not username_field or not password_field:
            raise RegioEnergieAuthError(
                "Login form has no recognisable user and password field - set "
                "the field names in the integration options"
            )

        payload[username_field] = self._username
        payload[password_field] = self._password
        _LOGGER.debug("Submitting login to %s with fields %s", action, sorted(payload))

        try:
            async with self._session.post(
                URL(action, encoded=True), data=payload, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status >= 500:
                    raise RegioEnergieConnectionError(
                        f"Portal returned HTTP {response.status} on login"
                    )
                body = await response.text()
        except aiohttp.ClientError as err:
            raise RegioEnergieConnectionError(f"Login request failed: {err}") from err
        except TimeoutError as err:
            raise RegioEnergieConnectionError("Login request timed out") from err

        # The same password field again means the portal bounced us back. A
        # form with another password field (e.g. "change password" on the
        # page behind the login) is not a rejection.
        answer = extract_login_form(body)
        if answer is not None and answer.password_field == password_field:
            raise RegioEnergieAuthError("Portal rejected the credentials")

        self._logged_in = True

    async def _async_get(self, url: str) -> tuple[str, str]:
        """GET a URL and return its body together with the final URL."""
        try:
            async with self._session.get(
                URL(url, encoded=True), timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status == 401 or response.status == 403:
                    raise RegioEnergieAuthError(
                        f"Portal returned HTTP {response.status} for {url}"
                    )
                if response.status >= 400:
                    raise RegioEnergieConnectionError(
                        f"Portal returned HTTP {response.status} for {url}"
                    )
                return await response.text(), str(response.url)
        except aiohttp.ClientError as err:
            raise RegioEnergieConnectionError(
                f"Request to {url} failed: {err}"
            ) from err
        except TimeoutError as err:
            raise RegioEnergieConnectionError(f"Request to {url} timed out") from err
