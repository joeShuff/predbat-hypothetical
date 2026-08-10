# Predbat Tariff Savings Plugin

`tariff_savings_plugin.py` is a Predbat plugin that answers:

> "Given the actual electricity my house used yesterday, what would that exact load have cost if I had bought every kWh from the grid on another tariff?"

It then compares that hypothetical cost with Predbat's actual daily energy cost:

**actual net cost = import cost - export income**

Standing charges are deliberately excluded.

## Features

- Uses Predbat's built-in `load_minutes` actual house-load tracking.
- Aggregates the load into local half-hour periods.
- Prices those periods against historical import rates.
- Supports the same main tariff sources as `compare_list`:
  - `rates_import`
  - `rates_import_octopus_url`
  - `metric_octopus_import`
  - `metric_energidataservice_import`
  - `metric_stromligning_import_today` / `metric_stromligning_import_tomorrow`
- Can copy the existing `compare_list`, or use a separate custom tariff list.
- Processes the previous local calendar day after 02:00.
- Stores daily results indefinitely using Predbat's Storage component, with no expiry.
- Stores the half-hourly load/rate/cost detail used for each calculation.
- Handles 23/25-hour local days using timestamps rather than assuming 48 slots.
- Provides:
  - `/tariff_savings`
  - `/api/tariff_savings`
- The web page supports arbitrary date ranges, so "last 5 years" and "all time" are possible.

## Installation

Copy `tariff_savings_plugin.py` into the same directory as Predbat's other Python files:

```text
apps/predbat/tariff_savings_plugin.py
```

The plugin system discovers files ending in `_plugin.py` automatically.

Restart Predbat after copying it.

## Configuration

The simplest configuration is to reuse your existing comparison list:

```yaml
tariff_savings:
  enabled: true
  process_hour: 2

  tariffs:
    mode: compare_list
    import_only: true
```

The plugin treats `compare_list` as the source of tariff definitions. Export-rate fields are ignored.

### Custom tariff list

If you don't want to use `compare_list`:

```yaml
tariff_savings:
  enabled: true
  process_hour: 2

  tariffs:
    mode: custom
    list:
      - id: agile
        name: Octopus Agile
        rates_import_octopus_url: "https://..."

      - id: fixed
        name: Fixed tariff
        rates_import:
          - rate: 8.0
            start: "00:00:00"
            end: "07:00:00"
          - rate: 30.0
            start: "07:00:00"
            end: "00:00:00"
```

`process_hour` defaults to `2`. The plugin actually runs on the first Predbat update at or after that hour, so a restart at 02:30 will still process yesterday.

## Data retention

There is intentionally **no retention setting**.

The plugin uses Predbat's Storage component with no expiry. Finalised history is kept forever.

A daily record contains:

- date
- total actual house load
- actual import cost
- actual export income
- actual net energy cost
- hypothetical cost for each tariff
- saving for each tariff
- every half-hour load/rate/cost input used in the calculation

This is designed for multi-year solar/battery ownership tracking.

## Web UI

Open the Predbat web interface and visit:

```text
/tariff_savings
```

The page supports custom date ranges. Leaving both dates blank shows all retained history.

For example:

```text
From: 2021-08-10
To:   2026-08-10
```

will show the five-year total for the selected tariff.

## Important limitation for newly installed copies

The plugin cannot recreate years of history that Predbat itself no longer has access to. It starts retaining its own detailed historical records when installed.

It does, however, use Predbat's existing historical load/rate data to process the previous day, so it does not require a second load sensor.

## Current implementation note

The plugin stores the tariff rates used on each day. This is important: if a tariff configuration changes in the future, old calculations remain auditable against the rates that were actually used when the day was processed.
