import datetime
import sys
import types
import unittest
from unittest.mock import patch

# The repository's lightweight test venv intentionally does not install
# pysolar; scheduling does not exercise solar calculations.
pysolar = types.ModuleType("pysolar")
pysolar_solar = types.ModuleType("pysolar.solar")
pysolar_solar.get_altitude = lambda *args, **kwargs: 0
sys.modules.setdefault("pysolar", pysolar)
sys.modules.setdefault("pysolar.solar", pysolar_solar)

import sync_artwork


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        sync_artwork._AUTO_ON_LAST_ATTEMPT = None

    def test_window_crossing_midnight_uses_its_own_grace(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 25, 0, 30)
                return value.replace(tzinfo=tz) if tz else value

        with patch.object(sync_artwork.datetime, "datetime", FixedDateTime):
            self.assertTrue(
                sync_artwork.is_within_schedule_window("23:00", 2, "test")
            )
            self.assertFalse(
                sync_artwork.is_within_schedule_window("23:00", 1, "test")
            )

    def test_empty_schedule_is_disabled(self):
        self.assertFalse(sync_artwork.is_within_schedule_window("", 2, "test"))

    def test_auto_on_is_attempted_only_once_per_scheduled_day(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 24, 7, 2)
                return value.replace(tzinfo=tz) if tz else value

        with (
            patch.object(sync_artwork, "AUTO_ON_TIME", "07:00"),
            patch.object(sync_artwork, "SYNC_INTERVAL_MINUTES", 15),
            patch.object(sync_artwork.datetime, "datetime", FixedDateTime),
        ):
            self.assertTrue(sync_artwork.should_attempt_auto_on())
            self.assertFalse(sync_artwork.should_attempt_auto_on())

    def test_auto_on_does_not_retry_after_scheduled_cycle(self):
        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 24, 7, 30)
                return value.replace(tzinfo=tz) if tz else value

        with (
            patch.object(sync_artwork, "AUTO_ON_TIME", "07:00"),
            patch.object(sync_artwork, "SYNC_INTERVAL_MINUTES", 15),
            patch.object(sync_artwork.datetime, "datetime", FixedDateTime),
        ):
            self.assertFalse(sync_artwork.should_attempt_auto_on())


if __name__ == "__main__":
    unittest.main()
