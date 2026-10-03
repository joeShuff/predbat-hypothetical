# -----------------------------------------------------------------------------
# Predbat Tariff Savings Plugin
# -----------------------------------------------------------------------------
"""Historical "without solar/battery" tariff savings for Predbat.

At/after the configured processing hour (default 02:00), this plugin prices the
previous local calendar day's actual house load against each configured import
tariff.  It compares that hypothetical grid-only cost with Predbat's actual
daily net energy cost (import cost - export income), excluding standing charge.

History is retained indefinitely in Predbat's Storage component.  Daily records
contain the half-hourly load/rate calculation so long-term results remain
auditable even if the tariff configuration later changes.
"""

import copy
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from plugin_system import PredBatPlugin as _PredBatPluginBase
from ha import run_async

UTC = timezone.utc
MODULE = "tariff_savings"
HISTORY_FILE = "history"
CURRENT_FILE = "current"
VERSION = 1


class TariffSavingsPlugin(_PredBatPluginBase):
    """Persistently compare actual load against historical tariff costs."""

    def __init__(self, base):
        super().__init__(base)
        self.base = base
        self.history = {"version": VERSION, "days": {}}
        self.current = {}
        self.loaded = False
        self.last_attempt_date = None
        self.processing = False

    def register_hooks(self, plugin_system):
        self.log("TariffSavings: register_hooks() called - registering hooks")
        plugin_system.register_hook("on_init", self.on_init)
        plugin_system.register_hook("on_update", self.on_update)
        plugin_system.register_hook("on_web_start", self.on_web_start)
        self.log("TariffSavings: hooks registered (on_init, on_update, on_web_start)")

    # ------------------------------------------------------------------
    # Configuration / storage
    # ------------------------------------------------------------------

    def _config(self):
        config = self.base.get_arg("tariff_savings", {})
        return config if isinstance(config, dict) else {}

    def _process_hour(self):
        value = self._config().get("process_hour", 2)
        try:
            return max(0, min(23, int(value)))
        except (ValueError, TypeError):
            return 2

    def _tariffs(self):
        config = self._config()
        tariffs = config.get("tariffs", {})
        if not isinstance(tariffs, dict):
            tariffs = {}

        mode = tariffs.get("mode", "compare_list")
        if mode == "custom":
            items = tariffs.get("list", [])
        else:
            items = self.base.get_arg("compare_list", [])

        if not isinstance(items, list):
            return []

        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            tariff = copy.deepcopy(item)
            tariff_id = str(tariff.get("id") or tariff.get("name") or "tariff")
            tariff["id"] = tariff_id
            # This comparison answers "what would my load have cost if I'd
            # imported all of it on tariff X, with no solar/battery offset?"
            # Export rates are never relevant to that question. No filtering
            # is needed here: downstream code (rate fetching, cost calc) only
            # ever reads import-related keys (id, name, rates_import*,
            # metric_*import*), so any export/unrelated keys just sit unused.
            result.append(tariff)
        return result

    def _storage(self):
        components = getattr(self.base, "components", None)
        return components.get_component("storage") if components else None

    def _load_storage(self):
        storage = self._storage()
        if not storage:
            self.log("TariffSavings: Storage component unavailable")
            return
        try:
            history = run_async(storage.load(MODULE, HISTORY_FILE))
            current = run_async(storage.load(MODULE, CURRENT_FILE))
            if isinstance(history, dict) and isinstance(history.get("days"), dict):
                self.history = history
            if isinstance(current, dict):
                self.current = current
        except Exception as exc:  # pylint: disable=broad-except
            self.log("TariffSavings: Failed to load history: {}".format(exc))
        self.loaded = True

    def _save_history(self):
        storage = self._storage()
        if not storage:
            return False
        try:
            return bool(run_async(storage.save(MODULE, HISTORY_FILE, self.history, format="json")))
        except Exception as exc:  # pylint: disable=broad-except
            self.log("TariffSavings: Failed to save history: {}".format(exc))
            return False

    def _save_current(self):
        storage = self._storage()
        if not storage:
            return False
        try:
            return bool(run_async(storage.save(MODULE, CURRENT_FILE, self.current, format="json")))
        except Exception as exc:  # pylint: disable=broad-except
            self.log("TariffSavings: Failed to save current day: {}".format(exc))
            return False

    def on_init(self):
        self.log("TariffSavings: on_init() called")
        self._load_storage()
        self.log("TariffSavings: on_init() complete, loaded={}".format(self.loaded))

    # ------------------------------------------------------------------
    # Current-day capture
    # ------------------------------------------------------------------

    def _dashboard(self, suffix):
        values = getattr(self.base, "dashboard_values", {})
        return values.get(self.base.prefix + "." + suffix, {})

    @staticmethod
    def _numeric(value):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def _capture_current(self):
        """Persist the latest built-in Predbat load/cost tracking.

        load_energy_actual.results is Predbat's cumulative actual-load curve.
        We retain it so a restart around midnight cannot lose the previous day.
        """
        now = getattr(self.base, "now_utc", None)
        if not now:
            return
        day = now.date().isoformat()

        load_entity = self._dashboard("load_energy_actual")
        load_results = (load_entity.get("attributes") or {}).get("results", {})
        if not isinstance(load_results, dict):
            load_results = {}

        import_entity = self._dashboard("cost_today_import")
        export_entity = self._dashboard("cost_today_export")

        import_p = self._numeric(import_entity.get("state"))
        export_p = self._numeric(export_entity.get("state"))

        self.current = {
            "version": VERSION,
            "date": day,
            "captured_at": now.isoformat(),
            "load_results": load_results,
            "actual_import_cost_p": import_p,
            "actual_export_cost_p": export_p,
        }
        self._save_current()

    # ------------------------------------------------------------------
    # Actual load
    # ------------------------------------------------------------------

    def _load_slots_from_internal_history(self, target_date):
        """Build local half-hour load buckets from Predbat's raw load history.

        Predbat's load_minutes is its built-in incrementing load tracking.  It
        is indexed backwards from now, so deriving timestamps from UTC elapsed
        minutes also handles 23/25-hour local days correctly.
        """
        data = getattr(self.base, "load_minutes", None)
        now_local = getattr(self.base, "now_utc", None)
        if not data or not now_local:
            return {}

        now_abs = now_local.astimezone(UTC)
        local_tz = getattr(self.base, "local_tz", None)
        if local_tz is None:
            local_tz = now_local.tzinfo

        buckets = {}
        max_key = len(data) - 2 if hasattr(data, "__len__") else 0
        if max_key <= 0:
            return {}

        for key in range(max_key + 1):
            try:
                kwh = self.base.get_from_incrementing(data, key)
            except (AttributeError, TypeError, ValueError):
                continue
            if kwh <= 0:
                continue

            timestamp_utc = now_abs - timedelta(minutes=key)
            timestamp_local = timestamp_utc.astimezone(local_tz)
            if timestamp_local.date().isoformat() != target_date:
                continue

            minute = (timestamp_local.minute // 30) * 30
            bucket = timestamp_local.replace(
                minute=minute, second=0, microsecond=0
            )
            # Keep offset in the key so the repeated hour on the autumn DST
            # transition remains two distinct half-hour periods.
            key_str = bucket.isoformat()
            buckets[key_str] = buckets.get(key_str, 0.0) + float(kwh)

        return dict(sorted(buckets.items()))

    def _load_slots_from_snapshot(self, target_date):
        """Fallback for a restart where the in-memory load history is absent."""
        if self.current.get("date") != target_date:
            return {}

        results = self.current.get("load_results", {})
        if not isinstance(results, dict) or not results:
            return {}

        points = []
        for stamp, value in results.items():
            try:
                timestamp = datetime.fromisoformat(stamp)
                numeric = float(value)
            except (ValueError, TypeError):
                continue
            points.append((timestamp, numeric))
        points.sort()

        buckets = {}
        for index in range(1, len(points)):
            start, value = points[index - 1]
            _, end_value = points[index]
            delta = max(0.0, end_value - value)
            if delta <= 0:
                continue
            minute = (start.minute // 30) * 30
            bucket = start.replace(minute=minute, second=0, microsecond=0)
            key_str = bucket.isoformat()
            buckets[key_str] = buckets.get(key_str, 0.0) + delta
        return dict(sorted(buckets.items()))

    def _get_load_slots(self, target_date):
        slots = self._load_slots_from_internal_history(target_date)
        if slots:
            return slots
        return self._load_slots_from_snapshot(target_date)

    # ------------------------------------------------------------------
    # Actual cost
    # ------------------------------------------------------------------

    def _history_entity_final(self, entity_id, target_date, days=2):
        """Find the latest state for an entity on a local calendar day."""
        try:
            history = self.base.get_history_wrapper(
                entity_id=entity_id, days=days, required=False
            )
        except Exception as exc:  # pylint: disable=broad-except
            self.log("TariffSavings: history lookup failed for {}: {}".format(entity_id, exc))
            history = None

        if not history:
            return None

        items = history[0] if isinstance(history, list) and history and isinstance(history[0], list) else history
        best = None
        best_time = None
        local_tz = getattr(self.base, "local_tz", None)

        for item in items or []:
            try:
                timestamp = item.get("last_updated")
                state = self._numeric(item.get("state"))
                if not timestamp or state is None:
                    continue
                dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if local_tz:
                    dt_local = dt.astimezone(local_tz)
                else:
                    dt_local = dt
                if dt_local.date().isoformat() != target_date:
                    continue
                if best_time is None or dt_local > best_time:
                    best_time = dt_local
                    best = state
            except (ValueError, TypeError):
                continue

        return best

    def _actual_cost(self, target_date):
        days_back = 2
        now = getattr(self.base, "now_utc", None)
        if now:
            try:
                target = datetime.strptime(target_date, "%Y-%m-%d").date()
                days_back = max(2, (now.date() - target).days + 1)
            except ValueError:
                pass

        import_p = self._history_entity_final(
            self.base.prefix + ".cost_today_import", target_date, days=days_back
        )
        export_p = self._history_entity_final(
            self.base.prefix + ".cost_today_export", target_date, days=days_back
        )

        if import_p is None or export_p is None:
            # Fallback to the last persisted current-day snapshot.
            if self.current.get("date") == target_date:
                import_p = self.current.get("actual_import_cost_p")
                export_p = self.current.get("actual_export_cost_p")

        if import_p is None or export_p is None:
            return None

        # cost_today_export is already negative in Predbat.
        return {
            "import_cost_p": round(float(import_p), 4),
            "export_cost_p": round(float(export_p), 4),
            "net_cost_p": round(float(import_p) + float(export_p), 4),
        }

    # ------------------------------------------------------------------
    # Tariff rate fetching
    # ------------------------------------------------------------------

    def _octopus_url_for_date(self, url, target_date):
        """Point a standard-unit-rates URL at a specific historical day.

        Without period_from/period_to, Octopus's API returns its default
        window (roughly "current and upcoming"), never arbitrary past days.
        We add/override those two query params so the call is pinned to the
        target local calendar day. Any other existing query params on the
        configured URL (e.g. page_size) are preserved.
        """
        pb = self.base
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
        start_local = datetime.combine(target, datetime.min.time())
        end_local = datetime.combine(target, datetime.max.time().replace(microsecond=0))

        local_tz = getattr(pb, "local_tz", None)
        if local_tz is not None:
            if hasattr(local_tz, "localize"):
                start_local = local_tz.localize(start_local)
                end_local = local_tz.localize(end_local)
            else:
                start_local = start_local.replace(tzinfo=local_tz)
                end_local = end_local.replace(tzinfo=local_tz)

        period_from = start_local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        period_to = end_local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query))
        params["period_from"] = period_from
        params["period_to"] = period_to
        new_query = urlencode(params)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

    def _fetch_import_rates(self, tariff, target_date):
        """Fetch import rates using the same source types as compare_list."""
        pb = self.base

        if tariff.get("rates_import_octopus_url"):
            url = pb.resolve_arg("rates_import_octopus_url", tariff["rates_import_octopus_url"], indirect=False)
            url = self._octopus_url_for_date(url, target_date)

            # download_octopus_rates() internally calls minute_data(mdata, 3,
            # self.midnight_utc, ...), which keeps only entries within +/-3
            # days of self.midnight_utc (today) and discards the rest. For a
            # target_date more than 3 days ago this would silently return an
            # empty/partial dict even with the right URL. Temporarily point
            # midnight_utc at the target day so the 3-day window is centred
            # on it instead, then re-key the result back onto the real
            # midnight_utc so offsets line up with what _rate_for_slot (which
            # always uses the real, current midnight_utc) expects.
            real_midnight_utc = pb.midnight_utc
            target_midnight_local = datetime.combine(
                datetime.strptime(target_date, "%Y-%m-%d").date(), datetime.min.time()
            )
            local_tz = getattr(pb, "local_tz", None)
            if local_tz is not None:
                if hasattr(local_tz, "localize"):
                    target_midnight_local = local_tz.localize(target_midnight_local)
                else:
                    target_midnight_local = target_midnight_local.replace(tzinfo=local_tz)
            target_midnight_utc = target_midnight_local.astimezone(UTC)

            try:
                pb.midnight_utc = target_midnight_utc
                rates = pb.download_octopus_rates(url)
            finally:
                pb.midnight_utc = real_midnight_utc

            if not rates or real_midnight_utc is None:
                return rates

            shift = int((target_midnight_utc - real_midnight_utc).total_seconds() / 60)
            if shift == 0:
                return rates
            return {key + shift: value for key, value in rates.items()}

        if tariff.get("metric_octopus_import"):
            entity_id = pb.resolve_arg("metric_octopus_import", tariff["metric_octopus_import"], indirect=False)
            return pb.fetch_octopus_rates(entity_id, adjust_key="is_intelligent_adjusted")

        if tariff.get("metric_energidataservice_import"):
            entity_id = pb.resolve_arg(
                "metric_energidataservice_import",
                tariff["metric_energidataservice_import"],
                indirect=False,
            )
            return pb.fetch_energidataservice_rates(
                entity_id, adjust_key="is_intelligent_adjusted"
            )

        if tariff.get("metric_stromligning_import_today") or tariff.get(
            "metric_stromligning_import_tomorrow"
        ):
            today = pb.resolve_arg(
                "metric_stromligning_import_today",
                tariff.get("metric_stromligning_import_today"),
            )
            tomorrow = pb.resolve_arg(
                "metric_stromligning_import_tomorrow",
                tariff.get("metric_stromligning_import_tomorrow"),
            )
            return pb.fetch_stromligning_rates(
                today, tomorrow, adjust_key="is_intelligent_adjusted"
            )

        if "rates_import" in tariff:
            # basic_rates uses the local calendar date for day-of-week/date
            # matching. Temporarily point it at the historical date.
            old_midnight = pb.midnight
            try:
                target_midnight = datetime.combine(
                    datetime.strptime(target_date, "%Y-%m-%d").date(),
                    datetime.min.time(),
                )
                local_tz = getattr(pb, "local_tz", target_midnight.tzinfo)
                if hasattr(local_tz, "localize"):
                    target_midnight = local_tz.localize(target_midnight)
                else:
                    target_midnight = target_midnight.replace(tzinfo=local_tz)
                pb.midnight = target_midnight
                return pb.basic_rates(tariff["rates_import"], "rates_import")
            finally:
                pb.midnight = old_midnight

        # A tariff with no rate source means "use the existing import rate".
        # This is useful for a custom tariff deliberately mirroring Predbat's
        # active tariff.
        return copy.deepcopy(getattr(pb, "rate_import", {}))

    def _rate_for_slot(self, rates, slot_start_local):
        if not rates:
            return None

        midnight = getattr(self.base, "midnight_utc", None)
        if midnight is None:
            return None

        # Rates are indexed by minutes relative to Predbat's current local
        # midnight. Work in UTC to make DST offsets unambiguous.
        offset = int(
            (
                slot_start_local.astimezone(UTC)
                - midnight.astimezone(UTC)
            ).total_seconds()
            / 60
        )

        value = rates.get(offset)
        if value is None:
            # Some fixed-rate configurations only expose a positive day.
            value = rates.get(offset % (24 * 60))
        try:
            return float(value) if value is not None else None
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Daily processing
    # ------------------------------------------------------------------

    def _process_day(self, target_date):
        if target_date in self.history.get("days", {}):
            return True

        load_slots = self._get_load_slots(target_date)
        if not load_slots:
            self.log("TariffSavings: No load data for {}".format(target_date))
            return False

        actual = self._actual_cost(target_date)
        if not actual:
            self.log("TariffSavings: No actual import/export cost for {}".format(target_date))
            return False

        tariffs = self._tariffs()
        if not tariffs:
            self.log("TariffSavings: No tariffs configured")
            return False

        day_record = {
            "date": target_date,
            "actual": actual,
            "load_kwh": round(sum(load_slots.values()), 6),
            "tariffs": {},
        }

        for tariff in tariffs:
            tariff_id = tariff["id"]
            name = tariff.get("name") or tariff_id
            try:
                rates = self._fetch_import_rates(tariff, target_date)
            except Exception as exc:  # pylint: disable=broad-except
                self.log(
                    "TariffSavings: Failed to fetch rates for {}: {}".format(
                        tariff_id, exc
                    )
                )
                continue

            slot_records = []
            hypothetical_p = 0.0

            for stamp, load_kwh in load_slots.items():
                try:
                    slot_start = datetime.fromisoformat(stamp)
                except (ValueError, TypeError):
                    continue
                rate = self._rate_for_slot(rates, slot_start)
                if rate is None:
                    self.log(
                        "TariffSavings: Missing rate for {} at {}".format(
                            tariff_id, stamp
                        )
                    )
                    continue

                cost_p = float(load_kwh) * rate
                hypothetical_p += cost_p
                slot_records.append(
                    {
                        "start": stamp,
                        "load_kwh": round(float(load_kwh), 6),
                        "rate_p": round(rate, 6),
                        "cost_p": round(cost_p, 6),
                    }
                )

            if not slot_records:
                continue

            # Don't silently call a partially priced day complete.
            if len(slot_records) != len(load_slots):
                self.log(
                    "TariffSavings: Incomplete rate coverage for {} on {}; not saving tariff result".format(
                        tariff_id, target_date
                    )
                )
                continue

            hypothetical_p = round(hypothetical_p, 4)
            day_record["tariffs"][tariff_id] = {
                "name": name,
                "hypothetical_cost_p": hypothetical_p,
                "actual_cost_p": actual["net_cost_p"],
                "saving_p": round(hypothetical_p - actual["net_cost_p"], 4),
                "slots": slot_records,
            }

        if not day_record["tariffs"]:
            return False

        self.history.setdefault("days", {})[target_date] = day_record
        if not self._save_history():
            self.history["days"].pop(target_date, None)
            return False

        self.log(
            "TariffSavings: Stored {} with {} tariff results, actual net cost {}p".format(
                target_date,
                len(day_record["tariffs"]),
                actual["net_cost_p"],
            )
        )
        return True

    def on_update(self):
        if not self.loaded:
            self._load_storage()

        self._capture_current()

        now = getattr(self.base, "now_utc", None)
        if not now or self.processing:
            return

        if now.hour < self._process_hour():
            return

        target_date = (now.date() - timedelta(days=1)).isoformat()
        if target_date == self.last_attempt_date and target_date in self.history.get("days", {}):
            return

        self.processing = True
        self.last_attempt_date = target_date
        try:
            self._process_day(target_date)
        finally:
            self.processing = False

    # ------------------------------------------------------------------
    # Web UI
    # ------------------------------------------------------------------

    def on_web_start(self):
        self.log("TariffSavings: on_web_start() called")
        components = getattr(self.base, "components", None)
        web_component = components.get_component("web") if components else None
        if not web_component:
            self.log("TariffSavings: Web component unavailable")
            return
        web_component.register_endpoint("/tariff_savings", self.web_page, "GET")
        web_component.register_endpoint("/api/tariff_savings", self.web_api, "GET")
        web_component.register_endpoint("/api/tariff_savings/run", self.web_run_now, "GET")
        web_component.register_endpoint("/api/tariff_savings/run_all", self.web_run_all, "GET")
        self.log(
            "TariffSavings: web endpoints registered "
            "(/tariff_savings, /api/tariff_savings, /api/tariff_savings/run, /api/tariff_savings/run_all)"
        )

    def _summarise(self, tariff_id, start=None, end=None):
        rows = []
        for day, record in sorted(self.history.get("days", {}).items()):
            if start and day < start:
                continue
            if end and day > end:
                continue
            tariff = (record.get("tariffs") or {}).get(tariff_id)
            if not tariff:
                continue
            rows.append(
                {
                    "date": day,
                    "load_kwh": record.get("load_kwh", 0),
                    "actual_cost_p": tariff.get(
                        "actual_cost_p", record.get("actual", {}).get("net_cost_p")
                    ),
                    "hypothetical_cost_p": tariff.get("hypothetical_cost_p"),
                    "saving_p": tariff.get("saving_p"),
                }
            )

        def total(key):
            return round(
                sum(float(row[key] or 0) for row in rows), 4
            )

        return {
            "days": rows,
            "days_count": len(rows),
            "load_kwh": total("load_kwh"),
            "actual_cost_p": total("actual_cost_p"),
            "hypothetical_cost_p": total("hypothetical_cost_p"),
            "saving_p": total("saving_p"),
        }

    async def web_api(self, request):
        query = parse_qs(request.query_string)
        tariff_id = query.get("tariff", [None])[0]

        tariff_ids = set()
        tariff_names = {}
        for record in self.history.get("days", {}).values():
            for key, tariff in (record.get("tariffs") or {}).items():
                tariff_ids.add(key)
                tariff_names[key] = tariff.get("name", key)

        if not tariff_id and tariff_ids:
            tariff_id = sorted(tariff_ids)[0]

        start = query.get("start", [None])[0]
        end = query.get("end", [None])[0]

        from aiohttp import web
        return web.json_response(
            {
                "version": VERSION,
                "tariffs": [
                    {"id": key, "name": tariff_names.get(key, key)}
                    for key in sorted(tariff_ids)
                ],
                "selected": tariff_id,
                "summary": self._summarise(tariff_id, start, end) if tariff_id else {},
            }
        )

    async def web_run_now(self, request):
        """Manually (re)process a day on demand, for setup/debugging.

        GET /api/tariff_savings/run?date=YYYY-MM-DD&force=1

        - date defaults to "yesterday" (local calendar day).
        - force=1 clears any existing stored result for that day first, so
          you can re-run after fixing config without waiting for a new day.
        - Log messages produced during processing are captured and returned
          in the response, so failures (missing load data, missing actual
          cost, no tariffs, incomplete rate coverage, etc.) are visible
          immediately instead of only in the Predbat log file.
        """
        from aiohttp import web

        query = parse_qs(request.query_string)
        date_param = query.get("date", [None])[0]
        force = query.get("force", ["0"])[0] in ("1", "true", "yes")

        if date_param:
            target_date = date_param
        else:
            now = getattr(self.base, "now_utc", None)
            if not now:
                return web.json_response(
                    {"ok": False, "error": "base.now_utc unavailable"}, status=500
                )
            target_date = (now.date() - timedelta(days=1)).isoformat()

        try:
            datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            return web.json_response(
                {"ok": False, "error": "date must be YYYY-MM-DD"}, status=400
            )

        if not self.loaded:
            self._load_storage()

        already_present = target_date in self.history.get("days", {})
        if force and already_present:
            self.history["days"].pop(target_date, None)

        captured = []
        original_log = self.log

        def _capture_log(message, *args, **kwargs):
            captured.append(str(message))
            return original_log(message, *args, **kwargs)

        self.log = _capture_log
        try:
            success = self._process_day(target_date)
        except Exception as exc:  # pylint: disable=broad-except
            captured.append("Exception: {}".format(exc))
            success = False
        finally:
            self.log = original_log

        record = self.history.get("days", {}).get(target_date)
        return web.json_response(
            {
                "ok": bool(success),
                "date": target_date,
                "forced": force,
                "already_present_before_run": already_present,
                "log": captured,
                "result": record,
            }
        )

    async def web_run_all(self, request):
        """Backfill every day Predbat/HA still has data for.

        GET /api/tariff_savings/run_all?force=1&max_days=60&stop_after_misses=3

        Walks backwards from yesterday, calling _process_day() for each date,
        until `stop_after_misses` consecutive dates in a row produce no load
        data (i.e. we've walked past the edge of whatever history Predbat's
        in-memory load_minutes / HA's recorder still retain), or `max_days`
        is reached, whichever comes first. A single missing day (e.g. a
        restart-related gap) does not stop the walk - only a run of misses
        does, so real gaps don't get mistaken for the edge of history.

        - force=1 reprocesses days that are already stored, overwriting them
          with the current tariff config (useful after a config change).
        - Without force, already-stored days are left untouched and reported
          as skipped, and only count towards the "no data" streak if they
          were never processable in the first place - already-present days
          never contribute to stop_after_misses.
        """
        from aiohttp import web

        query = parse_qs(request.query_string)
        force = query.get("force", ["0"])[0] in ("1", "true", "yes")
        try:
            max_days = int(query.get("max_days", ["60"])[0])
        except ValueError:
            max_days = 60
        try:
            stop_after_misses = int(query.get("stop_after_misses", ["3"])[0])
        except ValueError:
            stop_after_misses = 3
        max_days = max(1, min(max_days, 365))
        stop_after_misses = max(1, stop_after_misses)

        now = getattr(self.base, "now_utc", None)
        if not now:
            return web.json_response(
                {"ok": False, "error": "base.now_utc unavailable"}, status=500
            )

        if not self.loaded:
            self._load_storage()

        processed = []
        skipped = []
        failed = []
        consecutive_misses = 0
        cursor = now.date() - timedelta(days=1)
        days_walked = 0

        while days_walked < max_days and consecutive_misses < stop_after_misses:
            target_date = cursor.isoformat()
            days_walked += 1
            cursor -= timedelta(days=1)

            already_present = target_date in self.history.get("days", {})
            if already_present and not force:
                skipped.append(target_date)
                continue
            if already_present and force:
                self.history["days"].pop(target_date, None)

            captured = []
            original_log = self.log

            def _capture_log(message, *args, **kwargs):
                captured.append(str(message))
                return original_log(message, *args, **kwargs)

            self.log = _capture_log
            try:
                success = self._process_day(target_date)
            except Exception as exc:  # pylint: disable=broad-except
                captured.append("Exception: {}".format(exc))
                success = False
            finally:
                self.log = original_log

            if success:
                consecutive_misses = 0
                processed.append(target_date)
            else:
                consecutive_misses += 1
                failed.append({"date": target_date, "log": captured})

        return web.json_response(
            {
                "ok": True,
                "days_walked": days_walked,
                "stopped_reason": (
                    "no_more_data"
                    if consecutive_misses >= stop_after_misses
                    else "max_days_reached"
                ),
                "processed": processed,
                "processed_count": len(processed),
                "skipped_already_present": skipped,
                "failed": failed,
            }
        )

    async def web_page(self, request):
        from aiohttp import web

        tariff_ids = {}
        for record in self.history.get("days", {}).values():
            for key, tariff in (record.get("tariffs") or {}).items():
                tariff_ids[key] = tariff.get("name", key)

        options = "".join(
            '<option value="{}">{}</option>'.format(
                escape(key), escape(name)
            )
            for key, name in sorted(tariff_ids.items())
        )

        html = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Predbat Tariff Savings</title>
<style>
body{{font-family:system-ui,sans-serif;margin:20px;max-width:1100px}}
.controls{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}}
.card{{display:inline-block;min-width:180px;padding:16px;margin:4px;border:1px solid #ccc;border-radius:8px}}
.value{{font-size:1.6em;font-weight:600}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:7px;border-bottom:1px solid #ddd;text-align:right}}
th:first-child,td:first-child{{text-align:left}}
</style>
</head>
<body>
<h1>Tariff Savings</h1>
<p>Historical cost of your actual house load without solar/battery, compared
with Predbat's actual net energy cost. Standing charge is excluded.</p>
<div class="controls">
<label>Tariff <select id="tariff">{}</select></label>
<label>From <input id="start" type="date"></label>
<label>To <input id="end" type="date"></label>
<button onclick="loadData()">Update</button>
</div>
<div id="cards"></div>
<table><thead><tr>
<th>Date</th><th>Load</th><th>Actual</th><th>No system</th><th>Saving</th>
</tr></thead><tbody id="rows"></tbody></table>
<script>
async function loadData(){{
 const t=document.getElementById('tariff').value;
 const s=document.getElementById('start').value;
 const e=document.getElementById('end').value;
 const p=new URLSearchParams({{tariff:t}});
 if(s)p.set('start',s); if(e)p.set('end',e);
 const d=await fetch('/api/tariff_savings?'+p).then(r=>r.json());
 const x=d.summary||{{}};
 const gbp=v=>(Number(v||0)/100).toLocaleString('en-GB',{{style:'currency',currency:'GBP'}});
 document.getElementById('cards').innerHTML =
  `<div class="card">Load<div class="value">${{Number(x.load_kwh||0).toFixed(1)}} kWh</div></div>`+
  `<div class="card">Actual<div class="value">${{gbp(x.actual_cost_p)}}</div></div>`+
  `<div class="card">Without system<div class="value">${{gbp(x.hypothetical_cost_p)}}</div></div>`+
  `<div class="card">Saving<div class="value">${{gbp(x.saving_p)}}</div></div>`;
 document.getElementById('rows').innerHTML=(x.days||[]).map(r=>
  `<tr><td>${{r.date}}</td><td>${{Number(r.load_kwh||0).toFixed(2)}} kWh</td>`+
  `<td>${{gbp(r.actual_cost_p)}}</td><td>${{gbp(r.hypothetical_cost_p)}}</td>`+
  `<td>${{gbp(r.saving_p)}}</td></tr>`).join('');
}}
loadData();
</script>
</body>
</html>""".format(options)
        return web.Response(text=html, content_type="text/html")

    def shutdown(self):
        self._save_current()
        self._save_history()