# Regio Energie Smart Meter Import

[![hacs][hacs-badge]][hacs-url]

Home Assistant integration that imports smart meter load profiles from the
customer portal of **Regio Energie Solothurn** (`okp.regioenergie.ch`) and
turns them into long term statistics you can use on the **energy dashboard**.

Electricity, gas, water and district heating meters are supported, in any
combination — configure only the meters you actually have.

> The portal address is a setting, not a constant. If another Swiss utility
> runs the same portal software, point the integration at its address and it
> should work the same way. Reports of portals that do (or do not) work are
> welcome in the issue tracker.

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
2. **Meter** — name, type, meter number, unit, OBIS code and export type. The
   meter number is the long identifier shown in the portal, for example
   `CH1007201234500000000000000014185`. `1-1:1.29.0` / `WirkBezug` are the
   defaults used by Regio Energie Solothurn.

Both steps are verified against the portal before the entry is created, so a
wrong password or meter number is reported right away.

Further meters, the poll interval and the number of days per poll are managed
under **Configure** on the integration entry.

## Energy dashboard

**Settings → Dashboards → Energy**, then add the statistic that matches your
meter:

| Meter type       | Add it as                                 |
|------------------|-------------------------------------------|
| Electricity      | Grid consumption                          |
| Gas              | Gas consumption                           |
| Water            | Water consumption                         |
| District heating | Individual device / another energy source |

The statistics are named `regioenergie_import:<name>_import`, for example
`regioenergie_import:water_import`.

Data usually appears with roughly one day of delay — that is how fast the
utility publishes it, not a limitation of the integration.

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
OBIS code or the export type does not match. The probe script shows the raw
answer of the portal for a given meter.

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
