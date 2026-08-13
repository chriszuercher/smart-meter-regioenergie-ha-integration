"""Config and options flow for the Regio Energie Smart Meter Import."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import slugify

from .api import (
    RegioEnergieAuthError,
    RegioEnergieClient,
    RegioEnergieConnectionError,
    RegioEnergieError,
)
from .const import (
    ALL_UNITS,
    CONF_BASE_URL,
    CONF_DAYS_TO_FETCH,
    CONF_EXPORT_TYPE,
    CONF_LOGIN_URL,
    CONF_METER_NUMBER,
    CONF_METER_TYPE,
    CONF_METERS,
    CONF_OBIS_CODE,
    CONF_PASSWORD_FIELD,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_STATISTIC_ID,
    CONF_UNIT,
    CONF_USERNAME_FIELD,
    DEFAULT_BASE_URL,
    DEFAULT_DAYS_TO_FETCH,
    DEFAULT_EXPORT_TYPE,
    DEFAULT_OBIS_CODE,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DEFAULT_UNIT_BY_METER_TYPE,
    DOMAIN,
    MAX_DAYS_TO_FETCH,
    MAX_SCAN_INTERVAL_HOURS,
    METER_TYPE_ELECTRICITY,
    METER_TYPES,
    MIN_DAYS_TO_FETCH,
    MIN_SCAN_INTERVAL_HOURS,
    PORTAL_TIMEZONE,
    UNITS_BY_METER_TYPE,
)

_LOGGER = logging.getLogger(__name__)

# Window used to check that a meter actually returns data during setup.
PROBE_DAYS = 14


class InvalidMeter(Exception):
    """The portal answered, but not with usable readings for this meter."""


def build_statistic_id(name: str, taken: set[str]) -> str:
    """Build a unique, stable statistic id for a meter."""
    base = slugify(name) or "meter"
    candidate = f"{DOMAIN}:{base}_import"
    index = 2
    while candidate in taken:
        candidate = f"{DOMAIN}:{base}_{index}_import"
        index += 1
    return candidate


def _credentials_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Return the schema of the account step."""
    return vol.Schema(
        {
            vol.Required(
                CONF_BASE_URL, default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")
            ): TextSelector(TextSelectorConfig(autocomplete="username")),
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD, autocomplete="current-password"
                )
            ),
            vol.Optional(
                CONF_LOGIN_URL, default=defaults.get(CONF_LOGIN_URL, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Optional(
                CONF_USERNAME_FIELD, default=defaults.get(CONF_USERNAME_FIELD, "")
            ): TextSelector(),
            vol.Optional(
                CONF_PASSWORD_FIELD, default=defaults.get(CONF_PASSWORD_FIELD, "")
            ): TextSelector(),
        }
    )


def _meter_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Return the schema of the meter step."""
    meter_type = defaults.get(CONF_METER_TYPE, METER_TYPE_ELECTRICITY)
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME, default=defaults.get(CONF_NAME, "")
            ): TextSelector(),
            vol.Required(CONF_METER_TYPE, default=meter_type): SelectSelector(
                SelectSelectorConfig(
                    options=list(METER_TYPES),
                    translation_key="meter_type",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_METER_NUMBER, default=defaults.get(CONF_METER_NUMBER, "")
            ): TextSelector(),
            vol.Required(
                CONF_UNIT,
                default=defaults.get(CONF_UNIT, DEFAULT_UNIT_BY_METER_TYPE[meter_type]),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(ALL_UNITS),
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_OBIS_CODE, default=defaults.get(CONF_OBIS_CODE, DEFAULT_OBIS_CODE)
            ): TextSelector(),
            vol.Required(
                CONF_EXPORT_TYPE,
                default=defaults.get(CONF_EXPORT_TYPE, DEFAULT_EXPORT_TYPE),
            ): TextSelector(),
        }
    )


def _clean_meter_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Trim the free text fields of the meter form."""
    return {
        key: value.strip() if isinstance(value, str) else value
        for key, value in user_input.items()
    }


def _validate_meter_input(meter: Mapping[str, Any]) -> dict[str, str]:
    """Return per-field errors for a meter form submission."""
    errors: dict[str, str] = {}
    if not meter.get(CONF_NAME):
        errors[CONF_NAME] = "required"
    if not meter.get(CONF_METER_NUMBER):
        errors[CONF_METER_NUMBER] = "required"
    if not meter.get(CONF_OBIS_CODE):
        errors[CONF_OBIS_CODE] = "required"
    if meter.get(CONF_UNIT) not in UNITS_BY_METER_TYPE[meter[CONF_METER_TYPE]]:
        errors[CONF_UNIT] = "invalid_unit"
    return errors


async def async_probe_portal(
    hass: HomeAssistant,
    credentials: Mapping[str, Any],
    options: Mapping[str, Any],
    meter: Mapping[str, Any] | None = None,
) -> None:
    """Log in to the portal and, if given a meter, fetch a sample of its data.

    Raises the api errors, or InvalidMeter when the export stays empty.
    """
    session = async_create_clientsession(hass)
    client = RegioEnergieClient(
        session,
        credentials.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        credentials[CONF_USERNAME],
        credentials[CONF_PASSWORD],
        login_url=options.get(CONF_LOGIN_URL),
        username_field=options.get(CONF_USERNAME_FIELD),
        password_field=options.get(CONF_PASSWORD_FIELD),
    )
    try:
        await client.async_login()
        if meter is None:
            return

        end = datetime.now(ZoneInfo(PORTAL_TIMEZONE)).date()
        readings = await client.async_fetch_readings(
            meter[CONF_METER_NUMBER],
            meter.get(CONF_OBIS_CODE, DEFAULT_OBIS_CODE),
            meter.get(CONF_EXPORT_TYPE, DEFAULT_EXPORT_TYPE),
            end - timedelta(days=PROBE_DAYS),
            end,
        )
        if not readings:
            raise InvalidMeter(f"No readings for meter {meter[CONF_METER_NUMBER]}")
    finally:
        await session.close()


async def async_probe_with_errors(
    hass: HomeAssistant,
    credentials: Mapping[str, Any],
    options: Mapping[str, Any],
    meter: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Run async_probe_portal and translate its errors into form errors."""
    try:
        await async_probe_portal(hass, credentials, options, meter)
    except RegioEnergieAuthError as err:
        _LOGGER.debug("Login failed: %s", err)
        return {"base": "invalid_auth"}
    except RegioEnergieConnectionError as err:
        _LOGGER.debug("Portal unreachable: %s", err)
        return {"base": "cannot_connect"}
    except InvalidMeter as err:
        _LOGGER.debug("Meter probe empty: %s", err)
        return {CONF_METER_NUMBER: "no_data"}
    except RegioEnergieError as err:
        _LOGGER.debug("Portal error: %s", err)
        return {"base": "unknown"}
    except Exception:
        _LOGGER.exception("Unexpected error while validating the portal account")
        return {"base": "unknown"}
    return {}


class RegioEnergieConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of a portal account."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._credentials: dict[str, Any] = {}
        self._login_options: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the portal account and verify it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            credentials = {
                CONF_BASE_URL: user_input[CONF_BASE_URL].strip().rstrip("/"),
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            login_options = {
                key: user_input.get(key, "").strip()
                for key in (CONF_LOGIN_URL, CONF_USERNAME_FIELD, CONF_PASSWORD_FIELD)
                if user_input.get(key, "").strip()
            }

            host = urlparse(credentials[CONF_BASE_URL]).netloc
            await self.async_set_unique_id(
                f"{host}::{credentials[CONF_USERNAME].lower()}"
            )
            self._abort_if_unique_id_configured()

            errors = await async_probe_with_errors(
                self.hass, credentials, login_options
            )
            if not errors:
                self._credentials = credentials
                self._login_options = login_options
                return await self.async_step_meter()

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the first meter and create the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            meter = _clean_meter_input(user_input)
            errors = _validate_meter_input(meter)
            if not errors:
                errors = await async_probe_with_errors(
                    self.hass, self._credentials, self._login_options, meter
                )
            if not errors:
                meter[CONF_STATISTIC_ID] = build_statistic_id(meter[CONF_NAME], set())
                return self.async_create_entry(
                    title=self._credentials[CONF_USERNAME],
                    data=self._credentials,
                    options={
                        **self._login_options,
                        CONF_DAYS_TO_FETCH: DEFAULT_DAYS_TO_FETCH,
                        CONF_SCAN_INTERVAL_HOURS: DEFAULT_SCAN_INTERVAL_HOURS,
                        CONF_METERS: [meter],
                    },
                )

        return self.async_show_form(
            step_id="meter",
            data_schema=_meter_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle credentials that stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            credentials = {
                **entry.data,
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            errors = await async_probe_with_errors(
                self.hass, credentials, entry.options
            )
            if not errors:
                self.hass.config_entries.async_update_entry(entry, data=credentials)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")
                    ): TextSelector(TextSelectorConfig(autocomplete="username")),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"username": entry.data.get(CONF_USERNAME, "")},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return RegioEnergieOptionsFlow(config_entry)


class RegioEnergieOptionsFlow(OptionsFlow):
    """Manage meters and polling settings of an existing entry."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Keep a private reference; `self.config_entry` only exists in newer cores."""
        self._entry = config_entry
        self._options: dict[str, Any] = dict(config_entry.options)
        self._meters: list[dict[str, Any]] = [
            dict(meter) for meter in config_entry.options.get(CONF_METERS, [])
        ]
        self._selected: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what can be changed."""
        menu = ["add_meter", "settings"]
        if self._meters:
            menu = ["add_meter", "edit_meter", "remove_meter", "settings"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_add_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add another meter to this account."""
        errors: dict[str, str] = {}

        if user_input is not None:
            meter = _clean_meter_input(user_input)
            errors = _validate_meter_input(meter)
            if not errors:
                errors = await async_probe_with_errors(
                    self.hass, self._entry.data, self._options, meter
                )
            if not errors:
                meter[CONF_STATISTIC_ID] = build_statistic_id(
                    meter[CONF_NAME],
                    {existing[CONF_STATISTIC_ID] for existing in self._meters},
                )
                self._meters.append(meter)
                return self._save()

        return self.async_show_form(
            step_id="add_meter",
            data_schema=_meter_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_edit_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the meter to edit."""
        if user_input is not None:
            self._selected = user_input[CONF_STATISTIC_ID]
            return await self.async_step_meter_details()

        return self.async_show_form(
            step_id="edit_meter",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATISTIC_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=self._meter_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_meter_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the settings of the selected meter."""
        index = self._selected_index()
        if index is None:
            return await self.async_step_init()

        current = self._meters[index]
        errors: dict[str, str] = {}

        if user_input is not None:
            meter = _clean_meter_input(user_input)
            errors = _validate_meter_input(meter)
            if not errors:
                errors = await async_probe_with_errors(
                    self.hass, self._entry.data, self._options, meter
                )
            if not errors:
                # The statistic id stays as it is, otherwise the history
                # collected so far would be orphaned.
                meter[CONF_STATISTIC_ID] = current[CONF_STATISTIC_ID]
                self._meters[index] = meter
                return self._save()

        return self.async_show_form(
            step_id="meter_details",
            data_schema=_meter_schema(user_input or current),
            errors=errors,
            description_placeholders={
                CONF_STATISTIC_ID: current[CONF_STATISTIC_ID],
                CONF_NAME: current.get(CONF_NAME, ""),
            },
        )

    async def async_step_remove_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove meters from this account."""
        if user_input is not None:
            removed = set(user_input[CONF_METERS])
            self._meters = [
                meter
                for meter in self._meters
                if meter[CONF_STATISTIC_ID] not in removed
            ]
            return self._save()

        return self.async_show_form(
            step_id="remove_meter",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_METERS, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._meter_options(),
                            mode=SelectSelectorMode.LIST,
                            multiple=True,
                        )
                    )
                }
            ),
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change polling and login details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            login_options = {
                key: str(user_input.get(key, "")).strip()
                for key in (CONF_LOGIN_URL, CONF_USERNAME_FIELD, CONF_PASSWORD_FIELD)
                if str(user_input.get(key, "")).strip()
            }
            errors = await async_probe_with_errors(
                self.hass, self._entry.data, login_options
            )
            if not errors:
                self._options = {
                    **login_options,
                    CONF_DAYS_TO_FETCH: int(user_input[CONF_DAYS_TO_FETCH]),
                    CONF_SCAN_INTERVAL_HOURS: int(user_input[CONF_SCAN_INTERVAL_HOURS]),
                }
                return self._save()

        options = self._options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DAYS_TO_FETCH,
                        default=options.get(CONF_DAYS_TO_FETCH, DEFAULT_DAYS_TO_FETCH),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_DAYS_TO_FETCH,
                            max=MAX_DAYS_TO_FETCH,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_SCAN_INTERVAL_HOURS,
                        default=options.get(
                            CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL_HOURS,
                            max=MAX_SCAN_INTERVAL_HOURS,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_LOGIN_URL, default=options.get(CONF_LOGIN_URL, "")
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                    vol.Optional(
                        CONF_USERNAME_FIELD,
                        default=options.get(CONF_USERNAME_FIELD, ""),
                    ): TextSelector(),
                    vol.Optional(
                        CONF_PASSWORD_FIELD,
                        default=options.get(CONF_PASSWORD_FIELD, ""),
                    ): TextSelector(),
                }
            ),
            errors=errors,
        )

    def _meter_options(self) -> list[dict[str, str]]:
        """Return the configured meters as selector options."""
        return [
            {
                "value": meter[CONF_STATISTIC_ID],
                "label": f"{meter.get(CONF_NAME, '?')} ({meter[CONF_METER_NUMBER]})",
            }
            for meter in self._meters
        ]

    def _selected_index(self) -> int | None:
        """Return the list index of the meter picked before."""
        for index, meter in enumerate(self._meters):
            if meter[CONF_STATISTIC_ID] == self._selected:
                return index
        return None

    def _save(self) -> ConfigFlowResult:
        """Write the options back; the entry reloads through its listener."""
        return self.async_create_entry(
            title="", data={**self._options, CONF_METERS: self._meters}
        )
