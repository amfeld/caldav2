from collections.abc import Iterable
from odoo.tests import TransactionCase, tagged
from odoo import Command
from unittest.mock import patch, MagicMock, DEFAULT
import icalendar
from pathlib import Path
from .common import CaldavTestCommon
from contextlib import contextmanager
from datetime import datetime, UTC, timedelta
import caldav

WEEKDAY_MAP = {
    0: "SUN",
    1: "MON",
    2: "TUE",
    3: "WED",
    4: "THU",
    5: "FRI",
    6: "SAT",
}


def _get_ics_path(filename):
    return Path(__file__).parent / "data" / filename


@contextmanager
def _patch_caldav_with_events_from_ics(
    ics_paths, user, last_modified=None, futurize=True
):
    with patch("caldav.DAVClient") as MockDAVClient:
        mock_client = MockDAVClient.return_value
        mock_calendars = {}

        def calendar_side_effect(url):
            if url not in mock_calendars:
                mock_cal = MagicMock()
                mock_cal.events = MagicMock(return_value=[])
                mock_cal.event_by_uid = MagicMock()
                mock_calendars[url] = mock_cal
            return mock_calendars[url]

        mock_client.calendar = calendar_side_effect

        # Get or create the mock calendar for this user
        mock_calendar = calendar_side_effect(user.caldav_calendar_url)

        def event_by_uid_side_effect(uid):
            for event in mock_calendar.events():
                if str(event.icalendar_component.get("uid")) == uid:
                    return event
            return DEFAULT

        ical_events = []
        if ics_paths:
            if not isinstance(ics_paths, Iterable):
                ics_paths = [ics_paths] if ics_paths else []
            for ics_path in ics_paths:
                with ics_path.open("rb") as file:
                    ical_content = file.read()
                ical_events.append(icalendar.Calendar.from_ical(ical_content))
        if last_modified:
            for event in ical_events:
                for subcomponent in event.subcomponents:
                    if subcomponent.name == "VEVENT":
                        subcomponent["last-modified"] = icalendar.vDate(last_modified)
                        subcomponent["dtstamp"] = icalendar.vDate(last_modified)
        if futurize:
            for event in ical_events:
                for subcomponent in event.subcomponents:
                    if subcomponent.name == "VEVENT":
                        start = subcomponent.get("dtstart") and subcomponent.decoded(
                            "dtstart"
                        )
                        end = subcomponent.get("dtend") and subcomponent.decoded(
                            "dtend"
                        )
                        if isinstance(start, datetime) and isinstance(end, datetime):
                            duration = end - start
                        else:
                            duration = timedelta(hours=1)
                        subcomponent["dtstart"] = icalendar.vDDDTypes(datetime.now())
                        subcomponent["dtend"] = icalendar.vDDDTypes(
                            datetime.now() + duration
                        )

        base_events = [event for event in ical_events if not event.get("recurrence-id")]
        for base_event in base_events:
            child_events = [
                event
                for event in ical_events
                if event.get("recurrence-id")
                and event.get("uid") == base_event.get("uid")
            ]
            for child_event in child_events:
                base_event.add_component(child_event)
            mock_calendar.add_event(base_event)
        caldav_events = [caldav.Event(data=event) for event in base_events]
        mock_calendar.events.return_value = caldav_events
        mock_calendar.event_by_uid.side_effect = event_by_uid_side_effect
        user._compute_is_caldav_enabled()
        yield


@tagged("post_install", "-at_install")
class TestCalendarEvent(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.users"].search([])._compute_is_caldav_enabled()
        cls.user_1_url = "https://mycaldav.test.com/test1calendar"
        cls.user_1 = cls._generate_user(
            "test1",
            caldav_username="user1",
            caldav_password="pass1",
            caldav_url=cls.user_1_url,
        )
        cls.user_2_url = "https://mycaldav.test.com/test2calendar"
        cls.user_2 = cls._generate_user(
            "test2",
            caldav_username="user2",
            caldav_password="pass2",
            caldav_url=cls.user_2_url,
        )
        cls.user_3_url = "https://mycaldav.test.com/test3calendar"
        cls.user_3 = cls._generate_user(
            "test3",
            caldav_username="user3",
            caldav_password="pass3",
            caldav_url=cls.user_3_url,
        )

    def test_basic_event_from_server_create(self):
        user = self.user_1
        ics_path = _get_ics_path("basic.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            current_events = self.env["calendar.event"].search([])
            self.env["calendar.event"].poll_caldav_server()
            events_after_sync = self.env["calendar.event"].search([])
            new_events = events_after_sync - current_events
            self.assertEqual(len(new_events), 1)

    def test_basic_past_event_from_server_no_create(self):
        user = self.user_1
        ics_path = _get_ics_path("basic.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user, futurize=False):
            current_events = self.env["calendar.event"].search([])
            self.env["calendar.event"].poll_caldav_server()
            events_after_sync = self.env["calendar.event"].search([])
            new_events = events_after_sync - current_events
            self.assertEqual(len(new_events), 0)

    def test_basic_event_from_server_update(self):
        user = self.user_1
        ics_path = _get_ics_path("basic.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()

        # Verify the event was created correctly
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        self.assertEqual(event.name, "Test")
        orig_start = event.start
        orig_stop = event.stop

        # Now update the event with the updated ICS data
        ics_path = _get_ics_path("basic_updated.ics")
        with _patch_caldav_with_events_from_ics(
            ics_path,
            user,
            last_modified=(datetime.now(UTC)),
        ):
            # Clear any caches to ensure fresh data
            self.env["calendar.event"].invalidate_model()
            self.env["calendar.event"].poll_caldav_server()

        # Refresh the event from the database to get updated values
        event.invalidate_recordset()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])

        # Verify the event was updated correctly
        self.assertEqual(event.name, "Test Updated")
        # This next one is just lazy avoiding the HTML stripping
        self.assertIn("Some note ...", event.description)
        self.assertGreater(event.start, orig_start)
        self.assertGreater(event.stop, orig_stop)

    def test_basic_event_from_server_delete(self):
        user = self.user_1
        ics_path = _get_ics_path("basic.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        # Passing None to ics_path means no events returned from server
        with _patch_caldav_with_events_from_ics(None, user):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        self.assertFalse(event)

    def test_recurring_from_server_create(self):
        user = self.user_1
        ics_path = _get_ics_path("test_recurring.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        events = self.env["calendar.event"].search(
            [("partner_id", "=", user.partner_id.id)]
        )
        self.assertEqual(len(events), 10)

    def test_multiple_attendees_event_from_server_create(self):
        user = self.user_1
        ics_path = _get_ics_path("test_multi_attendee.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        self.assertEqual(len(event.attendee_ids), 3)
        self.assertIn(user.partner_id, event.attendee_ids.partner_id)

    def test_multiple_attendees_event_from_server_update(self):
        user = self.user_1
        ics_path = _get_ics_path("test_multi_attendee.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        ics_path = _get_ics_path("test_multi_attendee_update.ics")
        with _patch_caldav_with_events_from_ics(
            ics_path, user, last_modified=datetime.now(UTC)
        ):
            self.env["calendar.event"].poll_caldav_server()
        self.assertEqual(len(event.attendee_ids), 2)
        self.assertIn(user.partner_id, event.attendee_ids.partner_id)

    def test_multiple_attendees_event_from_server_delete(self):
        user = self.user_1
        ics_path = _get_ics_path("test_multi_attendee.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        # Passing None as ics_path means no events returned from server
        with _patch_caldav_with_events_from_ics(None, user):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        self.assertFalse(event)

    def test_multiple_user_attendees_event_from_server_create(self):
        """Test event has:
        Organizer: user1 (test1@example.com)
        Attendees: user2 and user3 (test2@example.com, test3@example.com)
        """
        user1 = self.user_1
        user2 = self.user_2
        user3 = self.user_3
        ics_path = _get_ics_path("test_multi_user.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user1):
            self.env["calendar.event"].poll_caldav_server()
        with _patch_caldav_with_events_from_ics(ics_path, user2):
            self.env["calendar.event"].poll_caldav_server()
        with _patch_caldav_with_events_from_ics(ics_path, user3):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search(
            [("caldav_uid", "=", "2495546B-5C9A-4632-AAD3-A179EF83CF20")]
        )
        self.assertEqual(len(event), 1)
        # Make sure the event wasn't duplicated all over the place
        other_user_events = self.env["calendar.event"].search(
            [("user_id", "in", [user2.id, user3.id])]
        )
        self.assertFalse(other_user_events)
        self.assertIn(user2.partner_id, event.partner_ids)
        self.assertIn(user3.partner_id, event.partner_ids)

    def test_multiple_user_attendees_event_from_server_update(self):
        """Test event has (as in above test):
        Organizer: user1 (test1@example.com)
        Attendees: user2 and user3 (test2@example.com, test3@example.com)
        """
        user1 = self.user_1
        user2 = self.user_2
        user3 = self.user_3
        ics_path = _get_ics_path("test_multi_user.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user1):
            self.env["calendar.event"].poll_caldav_server()
        with _patch_caldav_with_events_from_ics(ics_path, user2):
            self.env["calendar.event"].poll_caldav_server()
        with _patch_caldav_with_events_from_ics(ics_path, user3):
            self.env["calendar.event"].poll_caldav_server()
        notification_method = "odoo.addons.calendar.models.calendar_attendee.Attendee._send_mail_to_attendees"
        # Now update it to remove one attendee
        # Shuffle the user polling order just to test more robustly
        ics_path = _get_ics_path("test_multi_user_update.ics")
        with _patch_caldav_with_events_from_ics(
            ics_path, user2, last_modified=datetime.now(UTC)
        ), patch(notification_method) as mock_notification_method:
            self.env["calendar.event"].poll_caldav_server()
            mock_notification_method.assert_not_called()
        with _patch_caldav_with_events_from_ics(
            ics_path, user3, last_modified=datetime.now(UTC)
        ), patch(notification_method) as mock_notification_method:
            self.env["calendar.event"].poll_caldav_server()
            mock_notification_method.assert_not_called()
        with _patch_caldav_with_events_from_ics(
            ics_path, user1, last_modified=datetime.now(UTC)
        ), patch(notification_method) as mock_notification_method:
            self.env["calendar.event"].poll_caldav_server()
            mock_notification_method.assert_not_called()
        event = self.env["calendar.event"].search(
            [("caldav_uid", "=", "2495546B-5C9A-4632-AAD3-A179EF83CF20")]
        )
        self.assertIn(user3.partner_id, event.partner_ids)
        self.assertNotIn(user2.partner_id, event.partner_ids)
        self.assertEqual(len(event.attendee_ids), 2)

    def _create_multi_user_test_event(self):
        return (
            self.env["calendar.event"]
            .with_user(self.user_1)
            .create(
                {
                    "name": "Test event",
                    "partner_ids": [
                        Command.set(
                            [
                                self.user_2.partner_id.id,
                                self.user_3.partner_id.id,
                                self.user_1.partner_id.id,
                            ]
                        ),
                        Command.create(
                            {
                                "name": "Test partner",
                                "email": "testpartner@example.com",
                            }
                        ),
                    ],
                    "start": datetime.now() + timedelta(days=2),
                    "stop": datetime.now() + timedelta(days=2, hours=1),
                }
            )
        )

    @contextmanager
    def _patch_all_3_users_davclients(self):
        with patch("caldav.DAVClient") as MockDAVClient:
            (self.user_1 | self.user_2 | self.user_3)._compute_is_caldav_enabled()

            mock_client = MockDAVClient.return_value
            mock_calendar = MagicMock()
            mock_event_by_uid = MagicMock()
            mock_client.calendar.return_value = mock_calendar
            mock_calendar.events.return_value = []
            mock_calendar.event_by_uid.return_value = mock_event_by_uid
            yield mock_client, mock_calendar
