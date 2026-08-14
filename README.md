# Regio Energie Smart Meter Import

[![hacs][hacs-badge]][hacs-url]

Home Assistant integration that imports smart meter load profiles from the
customer portal of **Regio Energie Solothurn** (`okp.regioenergie.ch`) and
turns them into long term statistics you can use on the **energy dashboard**.

Electricity, gas, water and district heating meters are supported, in any
combination — configure only the meters you actually have. Optionally it also
imports what that consumption **costs**, including a seasonal tariff.

> **Tested with electricity and water only.** Gas and district heating are
> built in and selectable, but nobody has confirmed them against a real
> account yet — the OBIS code or the export type may well differ there. If you
> have such a meter, please
> [open an issue](https://github.com/cybersponge/smart-meter-regioenergie-ha-integration/issues)
> and say whether it worked and which values you used. Same for other Swiss
> utilities: the portal address is a setting, not a constant, so if another
> utility runs the same portal software this should work — reports welcome
> either way.

## How it works

The portal offers the load profile ("Lastgang") as a CSV export. The
integration signs in with your portal account, downloads the last few days for
each configured meter, aggregates the 15 minute values into full hours and
hands them to the Home Assistant recorder as **external statistics**.

Three details are worth knowing:

- The export timestamps the **end** of each interval, so the rows `01:15`,
  `01:30`, `01:45` and `02:00` all belong to the hour `01:00`. The integration
  shifts every timestamp back by 15 minutes before grouping, which is what
  makes the hourly buckets line up.
- Every poll re-reads a window of several days and recomputes the running
  totals on top of the last statistic before that window. Values a utility
  corrects after the fact are therefore picked up automatically.
- The export always covers the full requested range, so its tail is padded
  with empty values for intervals that are not measured yet. Those are skipped
  rather than imported as a consumption of zero, and the hour that is still
  running waits until it has fully elapsed.

No entities are created — the data goes straight into the statistics tables,
which is what the energy dashboard reads.

## Requirements

Home Assistant 2025.2 or newer, and an account for the web portal of your
utility.

## Installation

### HACS

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/cybersponge/smart-meter-regioenergie-ha-integration`
   with category **Integration**
3. Install **Regio Energie Smart Meter Import**, then restart Home Assistant

### Manual

Copy `custom_components/regioenergie_import` into your `config/custom_components`
directory and restart Home Assistant.

## Setup

**Settings → Devices & services → Add integration → Regio Energie Smart Meter Import**

1. **Portal account** — portal address (default `https://okp.regioenergie.ch`),
   username (your e-mail address) and password. The login form is discovered
   automatically — on `okp.regioenergie.ch` it lives at
   `/de/services/login.php` — so the three advanced fields only matter for
   portals where that fails (see Troubleshooting).
2. **Meter type** — electricity, gas, water or district heating. The unit and
   the price fields of the next step follow from it.
3. **Meter** — name, meter number, unit, OBIS code, export type and optionally
   the prices (see below). The unit is preset to the one the meter type is
   billed in (`kWh` for electricity, `m³` for water), and `1-1:1.29.0` /
   `WirkBezug` are the defaults used by Regio Energie Solothurn.

You find your meter numbers in the portal under **Lastgang**,
<https://okp.regioenergie.ch/de/services/lastgang.php> — one long identifier
per meter, in the form `CH1000000000000000000000000012345`.

Both the account and the meter are verified against the portal before the entry
is created, so a wrong password or meter number is reported right away.

Further meters, the poll interval and the number of days per poll are managed
under **Configure** on the integration entry.

## Energy dashboard

**Settings → Dashboards → Energy**, then add the statistic that matches your
meter:

| Meter type                  | Add it as                                 |
|-----------------------------|-------------------------------------------|
| Electricity                 | Grid consumption                          |
| Water                       | Water consumption                         |
| Gas (untested)              | Gas consumption                           |
| District heating (untested) | Individual device / another energy source |

The statistics are named `regioenergie_import:<name>_import`, for example
`regioenergie_import:water_import`.

Data usually appears with roughly one day of delay — that is how fast the
utility publishes it, not a limitation of the integration.

## Costs

Each meter takes two optional prices, in the currency configured in Home
Assistant:

| Field                          | Meaning                                                            |
|--------------------------------|--------------------------------------------------------------------|
| Price per unit                 | Applies all year, unless a winter price is given.                  |
| Winter price, October to March | Higher rate of the winter half year. Leave empty if there is none. |

Enter a price and the integration imports a **second statistic** per meter,
`regioenergie_import:<name>_cost`, holding the running cost. In the energy
dashboard, pick it under the source's cost setting as **"Use a statistic
tracking total costs"**.

Two things worth knowing:

- **The other two cost options do not work here.** "Use a static price" and
  "Use an entity with current price" are computed by an entity that watches the
  *state* of the consumption entity — and an external statistic has no entity
  and no state. That is why the integration calculates the cost itself.
- **Every hour is priced with the tariff of its own month**, so a backfill of
  past years is billed correctly rather than at today's price. Changing a price
  later re-prices all hours the next polls re-read; to re-price the whole
  history, raise *Days per poll* once (see Options).

For **water**, add the water and the wastewater price together — that sum is
the real price per m³. For **electricity**, Regio Energie bills April to
September at a lower rate and October to March at a higher one, so fill in both
fields.

## Options

| Option                        | Default | Meaning                                                         |
|-------------------------------|---------|-----------------------------------------------------------------|
| Days per poll                 | 7       | How far back each poll reads; the overlap picks up corrections. |
| Poll interval (hours)         | 6       | How often the portal is contacted.                              |
| Login page / form field names | empty   | Overrides for the login form discovery.                         |

## Troubleshooting

**"The portal rejected these credentials, or the login form could not be
found."** — first check the credentials in a browser. If they are correct, the
login form was probably not recognised. Run the bundled probe script from any
machine that can reach the portal:

```bash
python3 tools/probe_portal.py --username YOU --meter YOUR_METER_NUMBER
```

It has no dependencies beyond the standard library and prints the login form it
finds, the field names, and the first lines of the CSV export. Put the reported
values into the advanced login fields of the config flow.

**"The portal returned no readings for this meter."** — the meter number, the
OBIS code or the export type does not match. Compare the number against the
list under <https://okp.regioenergie.ch/de/services/lastgang.php>; the probe
script shows the raw answer of the portal for a given meter.

**Debug logging**

```yaml
logger:
  logs:
    custom_components.regioenergie_import: debug
```

## Known limitations

- Statistics are written on full hours in Swiss local time. During the autumn
  daylight saving change the repeated hour is folded into a single hour: the
  total consumption is preserved, its split across those two hours is not.
- Removing a meter or the integration leaves its statistics in place. Delete
  them under **Developer tools → Statistics** if you want them gone.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest
```

The tests cover the CSV parsing, the hourly bucketing, the running totals and
the login form discovery — no portal account needed.

## Credits

Based on a cron script that read the same export and pushed it into Home
Assistant over the WebSocket API. The CSV parsing and the hourly bucketing are
ported from it.

## License

MIT — see [LICENSE](LICENSE).

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
