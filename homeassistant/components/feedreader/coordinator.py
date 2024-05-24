"""Data update coordinator for RSS/Atom feeds."""

from __future__ import annotations

from calendar import timegm
from datetime import datetime, timedelta
from logging import getLogger
import os
import pickle
from time import gmtime, struct_time

import feedparser

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DELAY_SAVE, DOMAIN, EVENT_FEEDREADER, STORAGE_VERSION

_LOGGER = getLogger(__name__)


class FeedReaderCoordinator(DataUpdateCoordinator[None]):
    """Abstraction over Feedparser module."""

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        scan_interval: timedelta,
        max_entries: int,
        storage: StoredData,
    ) -> None:
        """Initialize the FeedManager object, poll as per scan interval."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=f"{DOMAIN} {url}",
            update_interval=scan_interval,
        )
        self._url = url
        self._max_entries = max_entries
        self._feed: feedparser.FeedParserDict | None = None
        self._firstrun = True
        self._storage = storage
        self._last_entry_timestamp: struct_time | None = None
        self._has_published_parsed = False
        self._has_updated_parsed = False
        self._event_type = EVENT_FEEDREADER
        self._feed_id = url
        _LOGGER.debug(
            "coordinator initi with url:%s scan_interval:%s", url, scan_interval
        )

    def _log_no_entries(self) -> None:
        """Send no entries log at debug level."""
        _LOGGER.debug("No new entries to be published in feed %s", self._url)

    async def _async_update_data(self) -> None:
        """Update the feed and publish new entries to the event bus."""
        last_entry_timestamp = await self.hass.async_add_executor_job(self._update)
        if last_entry_timestamp:
            self._storage.async_put_timestamp(self._feed_id, last_entry_timestamp)

    def _update(self) -> struct_time | None:
        """Update the feed and publish new entries to the event bus."""
        _LOGGER.debug("Fetching new data from feed %s", self._url)
        self._feed = feedparser.parse(
            self._url,
            etag=None if not self._feed else self._feed.get("etag"),
            modified=None if not self._feed else self._feed.get("modified"),
        )
        if not self._feed:
            _LOGGER.error("Error fetching feed data from %s", self._url)
            return None
        # The 'bozo' flag really only indicates that there was an issue
        # during the initial parsing of the XML, but it doesn't indicate
        # whether this is an unrecoverable error. In this case the
        # feedparser lib is trying a less strict parsing approach.
        # If an error is detected here, log warning message but continue
        # processing the feed entries if present.
        if self._feed.bozo != 0:
            _LOGGER.warning(
                "Possible issue parsing feed %s: %s",
                self._url,
                self._feed.bozo_exception,
            )
        # Using etag and modified, if there's no new data available,
        # the entries list will be empty
        _LOGGER.debug(
            "%s entri(es) available in feed %s",
            len(self._feed.entries),
            self._url,
        )
        if not self._feed.entries:
            self._log_no_entries()
            return None

        self._filter_entries()
        self._publish_new_entries()

        _LOGGER.debug("Fetch from feed %s completed", self._url)

        if (
            self._has_published_parsed or self._has_updated_parsed
        ) and self._last_entry_timestamp:
            return self._last_entry_timestamp

        return None

    def _filter_entries(self) -> None:
        """Filter the entries provided and return the ones to keep."""
        assert self._feed is not None
        if len(self._feed.entries) > self._max_entries:
            _LOGGER.debug(
                "Processing only the first %s entries in feed %s",
                self._max_entries,
                self._url,
            )
            self._feed.entries = self._feed.entries[0 : self._max_entries]

    def _update_and_fire_entry(self, entry: feedparser.FeedParserDict) -> None:
        """Update last_entry_timestamp and fire entry."""
        # Check if the entry has a updated or published date.
        # Start from a updated date because generally `updated` > `published`.
        if "updated_parsed" in entry and entry.updated_parsed:
            # We are lucky, `updated_parsed` data available, let's make use of
            # it to publish only new available entries since the last run
            self._has_updated_parsed = True
            self._last_entry_timestamp = max(
                entry.updated_parsed, self._last_entry_timestamp
            )
        elif "published_parsed" in entry and entry.published_parsed:
            # We are lucky, `published_parsed` data available, let's make use of
            # it to publish only new available entries since the last run
            self._has_published_parsed = True
            self._last_entry_timestamp = max(
                entry.published_parsed, self._last_entry_timestamp
            )
        else:
            self._has_updated_parsed = False
            self._has_published_parsed = False
            _LOGGER.debug(
                "No updated_parsed or published_parsed info available for entry %s",
                entry,
            )
        entry.update({"feed_url": self._url})
        self.hass.bus.fire(self._event_type, entry)
        _LOGGER.debug("New event fired for entry %s", entry.get("link"))

    def _publish_new_entries(self) -> None:
        """Publish new entries to the event bus."""
        assert self._feed is not None
        new_entry_count = 0
        self._last_entry_timestamp = self._storage.get_timestamp(self._feed_id)
        if self._last_entry_timestamp:
            self._firstrun = False
        else:
            # Set last entry timestamp as epoch time if not available
            self._last_entry_timestamp = dt_util.utc_from_timestamp(0).timetuple()
        # locally cache self._last_entry_timestamp so that entries published at identical times can be processed
        last_entry_timestamp = self._last_entry_timestamp
        for entry in self._feed.entries:
            if (
                self._firstrun
                or (
                    "published_parsed" in entry
                    and entry.published_parsed > last_entry_timestamp
                )
                or (
                    "updated_parsed" in entry
                    and entry.updated_parsed > last_entry_timestamp
                )
            ):
                self._update_and_fire_entry(entry)
                new_entry_count += 1
            else:
                _LOGGER.debug("Already processed entry %s", entry.get("link"))
        if new_entry_count == 0:
            self._log_no_entries()
        else:
            _LOGGER.debug("%d entries published in feed %s", new_entry_count, self._url)
        self._firstrun = False


class StoredData:
    """Represent a data storage."""

    def __init__(self, hass: HomeAssistant, legacy_data_file: str) -> None:
        """Initialize data storage."""
        self._legacy_data_file = legacy_data_file
        self._data: dict[str, struct_time] = {}
        self.hass = hass
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, DOMAIN)

    async def async_setup(self) -> None:
        """Set up storage."""
        if not os.path.exists(self._store.path):
            # Remove the legacy store loading after deprecation period.
            data = await self.hass.async_add_executor_job(self._legacy_fetch_data)
        else:
            if (store_data := await self._store.async_load()) is None:
                return
            # Make sure that dst is set to 0, by using gmtime() on the timestamp.
            data = {
                feed_id: gmtime(datetime.fromisoformat(timestamp_string).timestamp())
                for feed_id, timestamp_string in store_data.items()
            }

        self._data = data

    def _legacy_fetch_data(self) -> dict[str, struct_time]:
        """Fetch data stored in pickle file."""
        _LOGGER.debug("Fetching data from legacy file %s", self._legacy_data_file)
        try:
            with open(self._legacy_data_file, "rb") as myfile:
                return pickle.load(myfile) or {}
        except FileNotFoundError:
            pass
        except (OSError, pickle.PickleError) as err:
            _LOGGER.error(
                "Error loading data from pickled file %s: %s",
                self._legacy_data_file,
                err,
            )

        return {}

    def get_timestamp(self, feed_id: str) -> struct_time | None:
        """Return stored timestamp for given feed id."""
        return self._data.get(feed_id)

    @callback
    def async_put_timestamp(self, feed_id: str, timestamp: struct_time) -> None:
        """Update timestamp for given feed id."""
        self._data[feed_id] = timestamp
        self._store.async_delay_save(self._async_save_data, DELAY_SAVE)

    @callback
    def _async_save_data(self) -> dict[str, str]:
        """Save feed data to storage."""
        return {
            feed_id: dt_util.utc_from_timestamp(timegm(struct_utc)).isoformat()
            for feed_id, struct_utc in self._data.items()
        }
