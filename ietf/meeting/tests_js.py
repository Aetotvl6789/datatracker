# Copyright The IETF Trust 2014-2020, All Rights Reserved
# -*- coding: utf-8 -*-


import time
import datetime
import shutil
import os
import re
from unittest import skipIf
import urllib.parse

import django
from django.utils.text import slugify
from django.utils.timezone import now
from django.db.models import F
import pytz

from django.conf import settings
from django.test.utils import override_settings

import debug                            # pyflakes:ignore

from ietf.doc.factories import DocumentFactory
from ietf.doc.models import State
from ietf.group import colors
from ietf.person.models import Person
from ietf.group.models import Group
from ietf.group.factories import GroupFactory
from ietf.meeting.factories import ( MeetingFactory, RoomFactory, SessionFactory, TimeSlotFactory,
                                     ProceedingsMaterialFactory, ScheduleFactory, ConstraintFactory )
from ietf.meeting.test_data import make_meeting_test_data, make_interim_meeting
from ietf.meeting.models import (Schedule, SchedTimeSessAssignment, Session,
                                 Room, TimeSlot, Constraint, ConstraintName,
                                 Meeting, SchedulingEvent, SessionStatusName)
from ietf.meeting.utils import add_event_info_to_session_qs
from ietf.utils.test_utils import assert_ical_response_is_valid
from ietf.utils.jstest import ( IetfSeleniumTestCase, ifSeleniumEnabled, selenium_enabled,
                                presence_of_element_child_by_css_selector )

if selenium_enabled():
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions
    from selenium.common.exceptions import NoSuchElementException, TimeoutException
    # from selenium.webdriver.common.keys import Keys


@ifSeleniumEnabled
@override_settings(MEETING_SESSION_LOCK_TIME=datetime.timedelta(minutes=10))
class EditMeetingScheduleTests(IetfSeleniumTestCase):
    def test_edit_meeting_schedule(self):
        meeting = make_meeting_test_data()

        schedule = Schedule.objects.filter(meeting=meeting, owner__user__username="plain").first()

        room1 = Room.objects.get(name="Test Room")
        slot1 = TimeSlot.objects.filter(meeting=meeting, location=room1, type='regular').order_by('time').first()
        slot1b = TimeSlot.objects.filter(meeting=meeting, location=room1, type='regular').order_by('time').last()
        self.assertNotEqual(slot1.pk, slot1b.pk)

        room2 = Room.objects.create(meeting=meeting, name="Test Room2", capacity=1)
        room2.session_types.add('regular')
        slot2 = TimeSlot.objects.create(
            meeting=meeting,
            type_id='regular',
            location=room2,
            duration=datetime.timedelta(hours=2),
            time=slot1.time - datetime.timedelta(minutes=10),
        )

        slot3 = TimeSlot.objects.create(
            meeting=meeting,
            type_id='regular',
            location=room2,
            duration=datetime.timedelta(hours=2),
            time=max(slot1.end_time(), slot2.end_time()) + datetime.timedelta(minutes=10),
        )
        
        slot4 = TimeSlot.objects.create(
            meeting=meeting,
            type_id='regular',
            location=room1,
            duration=datetime.timedelta(hours=2),
            time=slot1.time + datetime.timedelta(days=1),
        )
        
        s1, s2 = Session.objects.filter(meeting=meeting, type='regular')
        s2.requested_duration = slot2.duration + datetime.timedelta(minutes=10)
        s2.save()
        SchedTimeSessAssignment.objects.filter(session=s1).delete()

        s2b = SessionFactory(
            meeting=meeting,
            group=s2.group,
            attendees=10,
            requested_duration=datetime.timedelta(minutes=60),
            add_to_schedule=False,
        )

        SchedulingEvent.objects.create(
            session=s2b,
            status=SessionStatusName.objects.get(slug='appr'),
            by=Person.objects.get(name='(System)'),
        )

        Constraint.objects.create(
            meeting=meeting,
            source=s1.group,
            target=s2.group,
            name=ConstraintName.objects.get(slug="conflict"),
        )

        self.login()
        url = self.absreverse('ietf.meeting.views.edit_meeting_schedule', kwargs=dict(num=meeting.number, name=schedule.name, owner=schedule.owner_email()))
        self.driver.get(url)

        WebDriverWait(self.driver, 2).until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, '.edit-meeting-schedule')))

        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, '.session.purpose-regular')), 3)

        # select - show session info
        s2_element = self.driver.find_element(By.CSS_SELECTOR, '#session{}'.format(s2.pk))
        s2b_element = self.driver.find_element(By.CSS_SELECTOR, '#session{}'.format(s2b.pk))
        self.assertNotIn('other-session-selected', s2b_element.get_attribute('class'))
        s2_element.click()

        # other session for group should be flagged for highlighting
        s2b_element = self.driver.find_element(By.CSS_SELECTOR, '#session{}'.format(s2b.pk))
        self.assertIn('other-session-selected', s2b_element.get_attribute('class'))

        # other session for group should appear in the info panel
        session_info_container = self.driver.find_element(By.CSS_SELECTOR, '.session-info-container')
        self.assertIn(s2.group.acronym, session_info_container.find_element(By.CSS_SELECTOR, ".title").text)
        self.assertEqual(session_info_container.find_element(By.CSS_SELECTOR, ".other-session .time").text, "not yet scheduled")

        # deselect
        self.driver.find_element(By.CSS_SELECTOR, '.drop-target').click()

        self.assertEqual(session_info_container.find_elements(By.CSS_SELECTOR, ".title"), [])
        self.assertNotIn('other-session-selected', s2b_element.get_attribute('class'))

        # unschedule

        # we would like to do
        #
        # unassigned_sessions_element = self.driver.find_element(By.CSS_SELECTOR, '.unassigned-sessions')
        # ActionChains(self.driver).drag_and_drop(s2_element, unassigned_sessions_element).perform()
        #
        # but unfortunately, Selenium does not simulate drag and drop events, see
        #
        #  https://github.com/seleniumhq/selenium-google-code-issue-archive/issues/3604
        #
        # so for the time being we inject the Javascript workaround here and do it from JS
        #
        #  https://storage.googleapis.com/google-code-attachments/selenium/issue-3604/comment-9/drag_and_drop_helper.js

        self.driver.execute_script('!function(s){s.fn.simulateDragDrop=function(t){return this.each(function(){new s.simulateDragDrop(this,t)})},s.simulateDragDrop=function(t,a){this.options=a,this.simulateEvent(t,a)},s.extend(s.simulateDragDrop.prototype,{simulateEvent:function(t,a){var e="dragstart",n=this.createEvent(e);this.dispatchEvent(t,e,n),e="drop";var r=this.createEvent(e,{});r.dataTransfer=n.dataTransfer,this.dispatchEvent(s(a.dropTarget)[0],e,r),e="dragend";var i=this.createEvent(e,{});i.dataTransfer=n.dataTransfer,this.dispatchEvent(t,e,i)},createEvent:function(t){var a=document.createEvent("CustomEvent");return a.initCustomEvent(t,!0,!0,null),a.dataTransfer={data:{},setData:function(t,a){this.data[t]=a},getData:function(t){return this.data[t]}},a},dispatchEvent:function(t,a,e){t.dispatchEvent?t.dispatchEvent(e):t.fireEvent&&t.fireEvent("on"+a,e)}})}(jQuery);')

        self.driver.execute_script("jQuery('#session{}').simulateDragDrop({{dropTarget: '.unassigned-sessions .drop-target'}});".format(s2.pk))

        WebDriverWait(self.driver, 2).until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, '.unassigned-sessions #session{}'.format(s2.pk))))

        self.assertEqual(list(SchedTimeSessAssignment.objects.filter(session=s2, schedule=schedule)), [])

        # sorting unassigned
        sorted_pks = [s.pk for s in sorted([s1, s2, s2b], key=lambda s: (s.group.acronym, s.requested_duration, s.pk))]
        self.driver.find_element(By.CSS_SELECTOR, '[name=sort_unassigned] option[value=name]').click()
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, '.unassigned-sessions .drop-target #session{} + #session{} + #session{}'.format(*sorted_pks)))

        sorted_pks = [s.pk for s in sorted([s1, s2, s2b], key=lambda s: (s.group.parent.acronym, s.group.acronym, s.requested_duration, s.pk))]
        self.driver.find_element(By.CSS_SELECTOR, '[name=sort_unassigned] option[value=parent]').click()
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, '.unassigned-sessions .drop-target #session{} + #session{}'.format(*sorted_pks)))
        
        sorted_pks = [s.pk for s in sorted([s1, s2, s2b], key=lambda s: (s.requested_duration, s.group.parent.acronym, s.group.acronym, s.pk))]
        self.driver.find_element(By.CSS_SELECTOR, '[name=sort_unassigned] option[value=duration]').click()
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, '.unassigned-sessions .drop-target #session{} ~ #session{}'.format(*sorted_pks)))
        
        sorted_pks = [s.pk for s in sorted([s1, s2, s2b], key=lambda s: (int(bool(s.comments)), s.group.parent.acronym, s.group.acronym, s.requested_duration, s.pk))]
        self.driver.find_element(By.CSS_SELECTOR, '[name=sort_unassigned] option[value=comments]').click()
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, '.unassigned-sessions .drop-target #session{} + #session{}'.format(*sorted_pks)))

        # schedule
        self.driver.execute_script("jQuery('#session{}').simulateDragDrop({{dropTarget: '#timeslot{} .drop-target'}});".format(s2.pk, slot1.pk))

        WebDriverWait(self.driver, 2).until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, '#timeslot{} #session{}'.format(slot1.pk, s2.pk))))

        assignment = SchedTimeSessAssignment.objects.get(session=s2, schedule=schedule)
        self.assertEqual(assignment.timeslot, slot1)

        # timeslot constraint hints when selected
        s1_element = self.driver.find_element(By.CSS_SELECTOR, '#session{}'.format(s1.pk))
        s1_element.click()

        # violated due to constraints - both the timeslot and its timeslot label
        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '#timeslot{}.would-violate-hint'.format(slot1.pk)))
        # Find the timeslot label for slot1 - it's the first timeslot in the first room group
        slot1_roomgroup_elt = self.driver.find_element(By.CSS_SELECTOR,
            '.day-flow .day:first-child .room-group:nth-child(2)'  # count from 2 - first-child is the day label
        )
        self.assertTrue(
            slot1_roomgroup_elt.find_elements(By.CSS_SELECTOR,
                '.time-header > .time-label.would-violate-hint:first-child'
            ),
            'Timeslot header label should show a would-violate hint for a constraint violation'
        )

        # violated due to missing capacity
        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '#timeslot{}.would-violate-hint'.format(slot3.pk)))
        # Find the timeslot label for slot3 - it's the second timeslot in the second room group
        slot3_roomgroup_elt = self.driver.find_element(By.CSS_SELECTOR,
            '.day-flow .day:first-child .room-group:nth-child(3)'  # count from 2 - first-child is the day label
        )
        self.assertFalse(
            slot3_roomgroup_elt.find_elements(By.CSS_SELECTOR,
                '.time-header > .time-label.would-violate-hint:nth-child(2)'
            ),
            'Timeslot header label should not show a would-violate hint for room capacity violation'
        )

        # reschedule
        self.driver.execute_script("jQuery('#session{}').simulateDragDrop({{dropTarget: '#timeslot{} .drop-target'}});".format(s2.pk, slot2.pk))

        WebDriverWait(self.driver, 2).until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, '#timeslot{} #session{}'.format(slot2.pk, s2.pk))))

        assignment = SchedTimeSessAssignment.objects.get(session=s2, schedule=schedule)
        self.assertEqual(assignment.timeslot, slot2)

        # too many attendees warning
        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '#session{}.too-many-attendees'.format(s2.pk)))

        # overfull timeslot
        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '#timeslot{}.overfull'.format(slot2.pk)))

        # constraint hints
        s1_element.click()
        self.assertIn('would-violate-hint', s2_element.get_attribute('class'))
        constraint_element = s2_element.find_element(By.CSS_SELECTOR, ".constraints span[data-sessions=\"{}\"].would-violate-hint".format(s1.pk))
        self.assertTrue(constraint_element.is_displayed())

        # current constraint violations
        self.driver.execute_script("jQuery('#session{}').simulateDragDrop({{dropTarget: '#timeslot{} .drop-target'}});".format(s1.pk, slot1.pk))

        WebDriverWait(self.driver, 2).until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, '#timeslot{} #session{}'.format(slot1.pk, s1.pk))))

        constraint_element = s2_element.find_element(By.CSS_SELECTOR, ".constraints span[data-sessions=\"{}\"].violated-hint".format(s1.pk))
        self.assertTrue(constraint_element.is_displayed())

        # hide sessions in area
        self.assertTrue(s1_element.is_displayed())
        self.driver.find_element(By.CSS_SELECTOR, ".session-parent-toggles [value=\"{}\"]".format(s1.group.parent.acronym)).click()
        self.assertTrue(s1_element.is_displayed())  # should still be displayed
        self.assertIn('hidden-parent', s1_element.get_attribute('class'),
                      'Session should be hidden when parent disabled')
        s1_element.click()  # try to select
        self.assertNotIn('selected', s1_element.get_attribute('class'),
                         'Session should not be selectable when parent disabled')

        self.driver.find_element(By.CSS_SELECTOR, ".session-parent-toggles [value=\"{}\"]".format(s1.group.parent.acronym)).click()
        self.assertTrue(s1_element.is_displayed())
        self.assertNotIn('hidden-parent', s1_element.get_attribute('class'),
                         'Session should not be hidden when parent enabled')
        s1_element.click()  # try to select
        self.assertIn('selected', s1_element.get_attribute('class'),
                         'Session should be selectable when parent enabled')

        # hide timeslots
        modal_open = self.driver.find_element(By.CSS_SELECTOR, "#timeslot-toggle-modal-open")
        self.driver.execute_script("arguments[0].click();", modal_open)  # FIXME: not working:
        # modal_open.click()

        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, "#timeslot-group-toggles-modal").is_displayed())
        self.driver.find_element(By.CSS_SELECTOR, "#timeslot-group-toggles-modal [value=\"{}\"]".format("ts-group-{}-{}".format(slot2.time.strftime("%Y%m%d-%H%M"), int(slot2.duration.total_seconds() / 60)))).click()
        self.driver.find_element(By.CSS_SELECTOR, "#timeslot-group-toggles-modal [data-bs-dismiss=\"modal\"]").click()
        self.assertTrue(not self.driver.find_element(By.CSS_SELECTOR, "#timeslot-group-toggles-modal").is_displayed())

        # swap days
        self.driver.find_element(By.CSS_SELECTOR, ".day .swap-days[data-dayid=\"{}\"]".format(slot4.time.date().isoformat())).click()
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, "#swap-days-modal").is_displayed())
        self.driver.find_element(By.CSS_SELECTOR, "#swap-days-modal input[name=\"target_day\"][value=\"{}\"]".format(slot1.time.date().isoformat())).click()
        self.driver.find_element(By.CSS_SELECTOR, "#swap-days-modal button[type=\"submit\"]").click()

        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '#timeslot{} #session{}'.format(slot4.pk, s1.pk)),
                        'Session s1 should have moved to second meeting day')

        # swap timeslot column - put session in a differently-timed timeslot
        self.driver.find_element(By.CSS_SELECTOR,
            '.day .swap-timeslot-col[data-timeslot-pk="{}"]'.format(slot1b.pk)
        ).click()  # open modal on the second timeslot for room1
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, "#swap-timeslot-col-modal").is_displayed())
        self.driver.find_element(By.CSS_SELECTOR,
            '#swap-timeslot-col-modal input[name="target_timeslot"][value="{}"]'.format(slot4.pk)
        ).click()  # select room1 timeslot that has a session in it
        self.driver.find_element(By.CSS_SELECTOR, '#swap-timeslot-col-modal button[type="submit"]').click()

        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '#timeslot{} #session{}'.format(slot1b.pk, s1.pk)),
                        'Session s1 should have moved to second timeslot on first meeting day')

    def test_past_flags(self):
        """Test that timeslots and sessions in the past are marked accordingly

        Would also like to test that past-hint flags are applied when a session is dragged, but that
        requires simulating HTML5 drag-and-drop. Have not yet found a good way to do this.
        """
        wait = WebDriverWait(self.driver, 2)
        meeting = MeetingFactory(type_id='ietf')
        room = RoomFactory(meeting=meeting)

        # get current time in meeting time zone
        right_now = now().astimezone(
            pytz.timezone(meeting.time_zone)
        )
        if not settings.USE_TZ:
            right_now = right_now.replace(tzinfo=None)

        past_timeslots = [
            TimeSlotFactory(meeting=meeting, time=right_now - datetime.timedelta(hours=n),
                            duration=datetime.timedelta(hours=1), location=room)
            for n in range(1,4)
        ]
        future_timeslots = [
            TimeSlotFactory(meeting=meeting, time=right_now + datetime.timedelta(hours=n),
                            duration=datetime.timedelta(hours=1), location=room)
            for n in range(1,4)
        ]
        now_timeslots = [
            # timeslot just barely in the past (to avoid race conditions) but overlapping now
            TimeSlotFactory(meeting=meeting, time=right_now - datetime.timedelta(seconds=1),
                            duration=datetime.timedelta(hours=1), location=room),
            # next slot is < MEETING_SESSION_LOCK_TIME in the future
            TimeSlotFactory(meeting=meeting, time=right_now + datetime.timedelta(minutes=9),
                            duration=datetime.timedelta(hours=1), location=room)
        ]

        past_sessions = [
            SchedTimeSessAssignment.objects.create(
                schedule=meeting.schedule,
                timeslot=ts,
                session=SessionFactory(meeting=meeting, add_to_schedule=False),
            ).session
            for ts in past_timeslots
        ]
        future_sessions = [
            SchedTimeSessAssignment.objects.create(
                schedule=meeting.schedule,
                timeslot=ts,
                session=SessionFactory(meeting=meeting, add_to_schedule=False),
            ).session
            for ts in future_timeslots
        ]
        now_sessions = [
            SchedTimeSessAssignment.objects.create(
                schedule=meeting.schedule,
                timeslot=ts,
                session=SessionFactory(meeting=meeting, add_to_schedule=False),
            ).session
            for ts in now_timeslots
        ]

        url = self.absreverse('ietf.meeting.views.edit_meeting_schedule', kwargs=dict(num=meeting.number))
        self.login(username=meeting.schedule.owner.user.username)
        self.driver.get(url)

        past_flags = self.driver.find_elements(By.CSS_SELECTOR,
            ','.join('#timeslot{} .past-flag'.format(ts.pk) for ts in past_timeslots)
        )
        self.assertGreaterEqual(len(past_flags), len(past_timeslots) + len(past_sessions),
                                'Expected at least one flag for each past timeslot and session')

        now_flags = self.driver.find_elements(By.CSS_SELECTOR,
            ','.join('#timeslot{} .past-flag'.format(ts.pk) for ts in now_timeslots)
        )
        self.assertGreaterEqual(len(now_flags), len(now_timeslots) + len(now_sessions),
                                'Expected at least one flag for each "now" timeslot and session')

        future_flags = self.driver.find_elements(By.CSS_SELECTOR,
            ','.join('#timeslot{} .past-flag'.format(ts.pk) for ts in future_timeslots)
        )
        self.assertGreaterEqual(len(future_flags), len(future_timeslots) + len(future_sessions),
                                'Expected at least one flag for each future timeslot and session')

        wait.until(expected_conditions.presence_of_element_located(
            (By.CSS_SELECTOR, '#timeslot{}.past'.format(past_timeslots[0].pk))
        ))
        for flag in past_flags:
            self.assertTrue(flag.is_displayed(), 'Past timeslot or session not flagged as past')

        for flag in now_flags:
            self.assertTrue(flag.is_displayed(), '"Now" timeslot or session not flagged as past')

        for flag in future_flags:
            self.assertFalse(flag.is_displayed(), 'Future timeslot or session is flagged as past')

    def test_past_swap_days_buttons(self):
        """Swap days buttons should be hidden for past items"""
        wait = WebDriverWait(self.driver, 2)
        meeting = MeetingFactory(type_id='ietf', date=datetime.datetime.today() - datetime.timedelta(days=3), days=7)
        room = RoomFactory(meeting=meeting)

        # get current time in meeting time zone
        right_now = now().astimezone(
            pytz.timezone(meeting.time_zone)
        )
        if not settings.USE_TZ:
            right_now = right_now.replace(tzinfo=None)

        past_timeslots = [
            TimeSlotFactory(meeting=meeting, time=right_now - datetime.timedelta(days=n),
                            duration=datetime.timedelta(hours=1), location=room)
            for n in range(4)  # includes 0
        ]
        future_timeslots = [
            TimeSlotFactory(meeting=meeting, time=right_now + datetime.timedelta(days=n),
                            duration=datetime.timedelta(hours=1), location=room)
            for n in range(1,4)
        ]
        now_timeslots = [
            # timeslot just barely in the past (to avoid race conditions) but overlapping now
            TimeSlotFactory(meeting=meeting, time=right_now - datetime.timedelta(seconds=1),
                            duration=datetime.timedelta(hours=1), location=room),
            # next slot is < MEETING_SESSION_LOCK_TIME in the future
            TimeSlotFactory(meeting=meeting, time=right_now + datetime.timedelta(minutes=9),
                            duration=datetime.timedelta(hours=1), location=room)
        ]

        url = self.absreverse('ietf.meeting.views.edit_meeting_schedule', kwargs=dict(num=meeting.number))
        self.login(username=meeting.schedule.owner.user.username)
        self.driver.get(url)

        past_swap_days_buttons = self.driver.find_elements(By.CSS_SELECTOR,
            ','.join(
                '.swap-days[data-start="{}"]'.format(ts.time.date().isoformat()) for ts in past_timeslots
            )
        )
        self.assertEqual(len(past_swap_days_buttons), len(past_timeslots), 'Missing past swap days buttons')

        future_swap_days_buttons = self.driver.find_elements(By.CSS_SELECTOR,
            ','.join(
                '.swap-days[data-start="{}"]'.format(ts.time.date().isoformat()) for ts in future_timeslots
            )
        )
        self.assertEqual(len(future_swap_days_buttons), len(future_timeslots), 'Missing future swap days buttons')

        now_swap_days_buttons = self.driver.find_elements(By.CSS_SELECTOR,
            ','.join(
                '.swap-days[data-start="{}"]'.format(ts.time.date().isoformat()) for ts in now_timeslots
            )
        )
        # only one "now" button because both sessions are on the same day
        self.assertEqual(len(now_swap_days_buttons), 1, 'Missing "now" swap days button')

        wait.until(
            expected_conditions.presence_of_element_located(
                (By.CSS_SELECTOR, '.timeslot.past')  # wait until timeslots are updated by JS
            )
        )

        # check that swap buttons are disabled for past days
        self.assertFalse(
            any(button.is_displayed() for button in past_swap_days_buttons),
            'Past swap days buttons still visible for official schedule',
        )
        self.assertTrue(
            all(button.is_displayed() for button in future_swap_days_buttons),
            'Future swap days buttons not visible for official schedule',
        )
        self.assertFalse(
            any(button.is_displayed() for button in now_swap_days_buttons),
            '"Now" swap days buttons still visible for official schedule',
        )

        # Open the swap days modal to verify that past day radios are disabled.
        # Use a middle day because whichever day we click will be disabled as an
        # option to swap. If we used the first or last day, a fencepost error in
        # disabling options by date might be hidden.
        clicked_index = 1
        # scroll so the button we want to click is just below the navbar, otherwise it may
        # fall beneath the sessions panel
        navbar = self.driver.find_element_by_class_name('navbar')
        self.driver.execute_script(
            'window.scrollBy({top: %s, behavior: "instant"})' % (
                    future_swap_days_buttons[1].location['y'] - navbar.size['height']
            )
        )
        future_swap_days_buttons[clicked_index].click()
        try:
            modal = wait.until(
                expected_conditions.visibility_of_element_located(
                    (By.CSS_SELECTOR, '#swap-days-modal')
                )
            )
        except TimeoutException:
            self.fail('Modal never appeared')
        self.assertFalse(
            any(radio.is_enabled()
                for radio in modal.find_elements(By.CSS_SELECTOR, ','.join(
                'input[name="target_day"][value="{}"]'.format(ts.time.date().isoformat()) for ts in past_timeslots)
            )),
            'Past day is enabled in swap-days modal for official schedule',
        )
        # future_timeslots[:-1] in the next selector because swapping a day with itself is disabled
        enabled_timeslots = (ts for ts in future_timeslots if ts != future_timeslots[clicked_index])
        self.assertTrue(
            all(radio.is_enabled()
                for radio in modal.find_elements(By.CSS_SELECTOR, ','.join(
                'input[name="target_day"][value="{}"]'.format(ts.time.date().isoformat()) for ts in enabled_timeslots)
            )),
            'Future day is not enabled in swap-days modal for official schedule',
        )
        self.assertFalse(
            any(radio.is_enabled()
                for radio in modal.find_elements(By.CSS_SELECTOR, ','.join(
                'input[name="target_day"][value="{}"]'.format(ts.time.date().isoformat()) for ts in now_timeslots)
            )),
            '"Now" day is enabled in swap-days modal for official schedule',
        )

    def test_past_swap_timeslot_col_buttons(self):
        """Swap timeslot column buttons should be hidden for past items"""
        wait = WebDriverWait(self.driver, 2)
        meeting = MeetingFactory(type_id='ietf', date=datetime.datetime.today() - datetime.timedelta(days=3), days=7)
        room = RoomFactory(meeting=meeting)

        # get current time in meeting time zone
        right_now = now().astimezone(
            pytz.timezone(meeting.time_zone)
        )
        if not settings.USE_TZ:
            right_now = right_now.replace(tzinfo=None)

        past_timeslots = [
            TimeSlotFactory(meeting=meeting, time=right_now - datetime.timedelta(hours=n),
                            duration=datetime.timedelta(hours=1), location=room)
            for n in range(1,4)  # does not include 0 to avoid race conditions
        ]
        future_timeslots = [
            TimeSlotFactory(meeting=meeting, time=right_now + datetime.timedelta(hours=n),
                            duration=datetime.timedelta(hours=1), location=room)
            for n in range(1,4)
        ]
        now_timeslots = [
            # timeslot just barely in the past (to avoid race conditions) but overlapping now
            TimeSlotFactory(meeting=meeting, time=right_now - datetime.timedelta(seconds=1),
                            duration=datetime.timedelta(hours=1), location=room),
            # next slot is < MEETING_SESSION_LOCK_TIME in the future
            TimeSlotFactory(meeting=meeting, time=right_now + datetime.timedelta(minutes=9),
                            duration=datetime.timedelta(hours=1), location=room)
        ]

        url = self.absreverse('ietf.meeting.views.edit_meeting_schedule', kwargs=dict(num=meeting.number))
        self.login(username=meeting.schedule.owner.user.username)
        self.driver.get(url)

        past_swap_ts_buttons = self.driver.find_elements(By.CSS_SELECTOR,
            ','.join(
                '*[data-start="{}"] .swap-timeslot-col'.format(ts.utc_start_time().isoformat()) for ts in past_timeslots
            )
        )
        self.assertEqual(len(past_swap_ts_buttons), len(past_timeslots), 'Missing past swap timeslot col buttons')

        future_swap_ts_buttons = self.driver.find_elements(By.CSS_SELECTOR,
            ','.join(
                '*[data-start="{}"] .swap-timeslot-col'.format(ts.utc_start_time().isoformat()) for ts in future_timeslots
            )
        )
        self.assertEqual(len(future_swap_ts_buttons), len(future_timeslots), 'Missing future swap timeslot col buttons')

        now_swap_ts_buttons = self.driver.find_elements(By.CSS_SELECTOR,
            ','.join(
                '[data-start="{}"] .swap-timeslot-col'.format(ts.utc_start_time().isoformat()) for ts in now_timeslots
            )
        )
        self.assertEqual(len(now_swap_ts_buttons), len(now_timeslots), 'Missing "now" swap timeslot col buttons')

        wait.until(
            expected_conditions.presence_of_element_located(
                (By.CSS_SELECTOR, '.timeslot.past')  # wait until timeslots are updated by JS
            )
        )

        # check that swap buttons are disabled for past days
        self.assertFalse(
            any(button.is_displayed() for button in past_swap_ts_buttons),
            'Past swap timeslot col buttons still visible for official schedule',
        )
        self.assertTrue(
            all(button.is_displayed() for button in future_swap_ts_buttons),
            'Future swap timeslot col buttons not visible for official schedule',
        )
        self.assertFalse(
            any(button.is_displayed() for button in now_swap_ts_buttons),
            '"Now" swap timeslot col buttons still visible for official schedule',
        )

        # Open the swap days modal to verify that past day radios are disabled.
        # Use a middle day because whichever day we click will be disabled as an
        # option to swap. If we used the first or last day, a fencepost error in
        # disabling options by date might be hidden.
        clicked_index = 1
        self.driver.execute_script("arguments[0].click();", future_swap_ts_buttons[clicked_index])  # FIXME: not working:
        # future_swap_ts_buttons[clicked_index].click()
        try:
            modal = wait.until(
                expected_conditions.visibility_of_element_located(
                    (By.CSS_SELECTOR, '#swap-timeslot-col-modal')
                )
            )
        except TimeoutException:
            self.fail('Modal never appeared')
        self.assertFalse(
            any(radio.is_enabled()
                for radio in modal.find_elements(By.CSS_SELECTOR, ','.join(
                'input[name="target_timeslot"][value="{}"]'.format(ts.pk) for ts in past_timeslots)
            )),
            'Past timeslot is enabled in swap-timeslot-col modal for official schedule',
        )
        # future_timeslots[:-1] in the next selector because swapping a timeslot with itself is disabled
        enabled_timeslots = (ts for ts in future_timeslots if ts != future_timeslots[clicked_index])
        self.assertTrue(
            all(radio.is_enabled()
                for radio in modal.find_elements(By.CSS_SELECTOR, ','.join(
                'input[name="target_timeslot"][value="{}"]'.format(ts.pk) for ts in enabled_timeslots)
            )),
            'Future timeslot is not enabled in swap-timeslot-col modal for official schedule',
        )
        self.assertFalse(
            any(radio.is_enabled()
                for radio in modal.find_elements(By.CSS_SELECTOR, ','.join(
                'input[name="target_timeslot"][value="{}"]'.format(ts.pk) for ts in now_timeslots)
            )),
            '"Now" timeslot is enabled in swap-timeslot-col modal for official schedule',
        )

    def test_unassigned_sessions_sort(self):
        """Unassigned session sorting should behave correctly

        Sorting options and list of sort criteria
          name (name, duration, id)
          parent (parent, name, duration, id)
          duration (duration, parent, name, id)
          comments (presence of comments, parent, name, duration, id)
        """
        # Define helpers
        def sort_by_position(driver, sessions):
            """Helper to sort sessions by the position of their session element in the unscheduled box"""
            def _sort_key(sess):
                elt = driver.find_element(By.ID, 'session{}'.format(sess.pk))
                return (elt.location['y'], elt.location['x'])
            return sorted(sessions, key=_sort_key)

        wait = WebDriverWait(self.driver, 2)

        def wait_for_order(sessions, expected_order, fail_message):
            """Helper to wait for sorting to complete"""
            try:
                wait.until(
                    lambda driver: sort_by_position(driver, sessions) == expected_order,
                )
            except TimeoutException:
                pass  # Fall through to the assertion which will fail, don't throw a confusing timeout exception
            self.assertEqual(sort_by_position(self.driver, sessions), expected_order, fail_message)

        # Start the test here
        # set up several WGs in various areas, including no area.
        area_acronyms = ['A', 'B', 'C', 'D']
        areas = [GroupFactory(type_id='area', acronym=acro) for acro in area_acronyms]

        # now create WGs with acronyms that sort differently than by area (g00, g01, g02...)
        num = 0
        wgs = []
        group_acro = lambda n: 'g{:02d}'.format(n)
        for _ in range(2):
            wgs.append(GroupFactory(acronym=group_acro(num), type_id='wg', parent=None))
            num += 1
            for area in areas:
                wgs.append(GroupFactory(acronym=group_acro(num), type_id='wg', parent=area))
                num += 1

        # Create an IETF meeting...
        meeting = MeetingFactory(type_id='ietf')

        # ...add a room that has no timeslots to be sure it's handled...
        RoomFactory(meeting=meeting)

        # ...and sessions for the groups. Use durations that are in a different order than
        # area or name. The wgs list is in ascending acronym order, so use descending durations.
        sessions = []
        for n, wg in enumerate(wgs[::-1]):
            sessions.append(
                SessionFactory(
                    meeting=meeting,
                    group=wg,
                    requested_duration=datetime.timedelta(minutes=30 + 5 * n),
                    status_id='schedw',
                    add_to_schedule=False,
                )
            )

        # Finally, assign comments to some sessions. Assign every 3rd until we reach the end.
        # This should be a different sort than any of the other axes.
        for sess in sessions[::3]:
            sess.comments = 'special request'
            sess.save()

        url = self.absreverse('ietf.meeting.views.edit_meeting_schedule', kwargs=dict(num=meeting.number))
        self.login('secretary')
        self.driver.get(url)

        select = self.driver.find_element(By.NAME, 'sort_unassigned')
        options = {
            opt.get_attribute('value'): opt
            for opt in select.find_elements(By.TAG_NAME, 'option')
        }

        # check sorting by name
        options['name'].click()
        self.assertEqual(select.get_attribute('value'), 'name')
        expected_order = sorted(
            sessions,
            key=lambda s: (
                s.group.acronym,
                s.requested_duration,
            )
        )
        wait_for_order(sessions, expected_order, 'Failed to sort by name')

        # check sorting by parent
        options['parent'].click()
        self.assertEqual(select.get_attribute('value'), 'parent')
        expected_order = sorted(
            sessions,
            key=lambda s: (
                s.group.parent.acronym if s.group.parent else '',
                s.group.acronym,
                s.requested_duration,
            )
        )
        wait_for_order(sessions, expected_order, 'Failed to sort by parent')

        # check sorting by duration
        options['duration'].click()
        self.assertEqual(select.get_attribute('value'), 'duration')
        expected_order = sorted(
            sessions,
            key=lambda s: (
                s.requested_duration,
                s.group.parent.acronym if s.group.parent else '',
                s.group.acronym,
            )
        )
        wait_for_order(sessions, expected_order, 'Failed to sort by duration')

        # check sorting by comments
        options['comments'].click()
        self.assertEqual(select.get_attribute('value'), 'comments')
        expected_order = sorted(
            sessions,
            key=lambda s: (
                0 if len(s.comments) > 0 else 1,
                s.group.parent.acronym if s.group.parent else '',
                s.group.acronym,
                s.requested_duration,
            )
        )
        wait_for_order(sessions, expected_order, 'Failed to sort by comments')

    def test_unassigned_sessions_drop_target_visible_when_empty(self):
        """The drop target for unassigned sessions should not collapse to 0 size

        This test checks that issue #3174 has not regressed. A test that exercises
        moving items from the schedule into the unassigned-sessions area is needed,
        but as of 2021-05-04, Selenium does not support the HTML5 drag-and-drop
        event interface. See:

        https://github.com/seleniumhq/selenium-google-code-issue-archive/issues/3604
        https://gist.github.com/rcorreia/2362544

        Note, however, that the workarounds are inadequate - they do not handle
        all of the events needed by the editor.
        """
        # Set up a meeting and a schedule a plain user can edit
        schedule = ScheduleFactory(meeting__type_id='ietf', owner__user__username="plain")
        meeting = schedule.meeting

        # Open the editor
        self.login()
        url = self.absreverse(
            'ietf.meeting.views.edit_meeting_schedule',
            kwargs=dict(num=meeting.number, name=schedule.name, owner=schedule.owner_email())
        )
        self.driver.get(url)
        # Check that the drop target for unassigned sessions is actually empty
        drop_target = self.driver.find_element(By.CSS_SELECTOR,
            '.unassigned-sessions .drop-target'
        )
        self.assertEqual(len(drop_target.find_elements(By.CLASS_NAME, 'session')), 0,
                         'Unassigned sessions box is not empty, test is broken')

        # Check that the drop target has non-zero size
        self.assertGreater(drop_target.size['height'], 0,
                           'Drop target for unassigned sessions collapsed to 0 height')
        self.assertGreater(drop_target.size['width'], 0,
                           'Drop target for unassigned sessions collapsed to 0 width')

    def test_session_constraint_hints(self):
        """Selecting a session should mark conflicting sessions

        To test for recurrence of https://trac.ietf.org/trac/ietfdb/ticket/3327 need to have some constraints that
        do not conflict. Testing with only violated constraints does not exercise the code adequately.
        """
        meeting = MeetingFactory(type_id='ietf', date=datetime.date.today(), populate_schedule=False)
        TimeSlotFactory.create_batch(5, meeting=meeting)
        schedule = ScheduleFactory(meeting=meeting)
        sessions = SessionFactory.create_batch(5, meeting=meeting, add_to_schedule=False)
        groups = [s.group for s in sessions]

        # Now set up constraints
        # Get an arbitrary enabled group conflict ConstraintName
        constraint_names = meeting.enabled_constraint_names().filter(is_group_conflict=True)
        self.assertGreaterEqual(len(constraint_names), 2, 'Not enough constraint names enabled to perform test')

        # one-way conflict from group 0 to 1
        ConstraintFactory(meeting=meeting, name=constraint_names[0], source=groups[0], target=groups[1], person=None)

        # one-way conflict from group 2 to 0
        ConstraintFactory(meeting=meeting, name=constraint_names[0], source=groups[2], target=groups[0], person=None)

        # two-way conflict between groups 0 and 3
        ConstraintFactory(meeting=meeting, name=constraint_names[0], source=groups[0], target=groups[3], person=None)
        ConstraintFactory(meeting=meeting, name=constraint_names[0], source=groups[3], target=groups[0], person=None)

        # constraints that are not active when selecting sessions[0]
        ConstraintFactory(meeting=meeting, name=constraint_names[1], source=groups[1], target=groups[2], person=None)
        ConstraintFactory(meeting=meeting, name=constraint_names[1], source=groups[3], target=groups[4], person=None)

        url = self.absreverse('ietf.meeting.views.edit_meeting_schedule',
                              kwargs=dict(num=meeting.number, owner=schedule.owner.email(), name=schedule.name))
        self.login(schedule.owner.user.username)
        self.driver.get(url)
        session_elements = [self.driver.find_element(By.CSS_SELECTOR, f'#session{sess.pk}') for sess in sessions]
        session_elements[0].click()

        # All conflicting sessions should be flagged with the would-violate-hint class.
        self.assertIn('would-violate-hint', session_elements[1].get_attribute('class'),
                      'Constraint violation should be indicated on conflicting session')
        self.assertIn('would-violate-hint', session_elements[2].get_attribute('class'),
                      'Constraint violation should be indicated on conflicting session')
        self.assertIn('would-violate-hint', session_elements[3].get_attribute('class'),
                      'Constraint violation should be indicated on conflicting session')

        # And the non-conflicting session should not be flagged
        self.assertNotIn('would-violate-hint', session_elements[4].get_attribute('class'),
                         'Constraint violation should not be indicated on non-conflicting session')

@ifSeleniumEnabled
@skipIf(django.VERSION[0]==2, "Skipping test with race conditions under Django 2")
class ScheduleEditTests(IetfSeleniumTestCase):
    def testUnschedule(self):

        meeting = make_meeting_test_data()
        colors.fg_group_colors['FARFUT'] = 'blue'
        colors.bg_group_colors['FARFUT'] = 'white'
        
        self.assertEqual(SchedTimeSessAssignment.objects.filter(session__meeting=meeting, session__group__acronym='mars', schedule__name='test-schedule').count(),1)


        ss = list(SchedTimeSessAssignment.objects.filter(session__meeting__number=72,session__group__acronym='mars',schedule__name='test-schedule')) # pyflakes:ignore

        self.login()
        url = self.absreverse('ietf.meeting.views.edit_meeting_schedule',kwargs=dict(num='72',name='test-schedule',owner='plain@example.com'))
        self.driver.get(url)

        # driver.get() will wait for scripts to finish, but not ajax
        # requests.  Wait for completion of the permissions check:
        read_only_note = self.driver.find_element(By.ID, 'read_only')
        WebDriverWait(self.driver, 10).until(expected_conditions.invisibility_of_element(read_only_note), "Read-only schedule")

        s1 = Session.objects.filter(group__acronym='mars', meeting=meeting).first()
        selector = "#session_{}".format(s1.pk)
        WebDriverWait(self.driver, 30).until(expected_conditions.presence_of_element_located((By.CSS_SELECTOR, selector)), "Did not find %s"%selector)

        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, "#sortable-list #session_{}".format(s1.pk)), [])

        element = self.driver.find_element(By.ID, 'session_{}'.format(s1.pk))
        target  = self.driver.find_element(By.ID, 'sortable-list')
        ActionChains(self.driver).drag_and_drop(element,target).perform()

        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, "#sortable-list #session_{}".format(s1.pk)))

        time.sleep(0.1) # The API that modifies the database runs async

        self.assertEqual(SchedTimeSessAssignment.objects.filter(session__meeting__number=72,session__group__acronym='mars',schedule__name='test-schedule').count(),0)

@ifSeleniumEnabled
class SlideReorderTests(IetfSeleniumTestCase):
    def setUp(self):
        super(SlideReorderTests, self).setUp()
        self.session = SessionFactory(meeting__type_id='ietf', status_id='sched')
        self.session.sessionpresentation_set.create(document=DocumentFactory(type_id='slides',name='one'),order=1)
        self.session.sessionpresentation_set.create(document=DocumentFactory(type_id='slides',name='two'),order=2)
        self.session.sessionpresentation_set.create(document=DocumentFactory(type_id='slides',name='three'),order=3)

    def secr_login(self):
        self.login('secretary')

    #@override_settings(DEBUG=True)
    def testReorderSlides(self):
        return
        url = self.absreverse('ietf.meeting.views.session_details',
                  kwargs=dict(
                      num=self.session.meeting.number,
                      acronym = self.session.group.acronym,))
        self.secr_login()
        self.driver.get(url)        
        #debug.show('unicode(self.driver.page_source)')
        second = self.driver.find_element(By.CSS_SELECTOR, '#slides tr:nth-child(2)')
        third = self.driver.find_element(By.CSS_SELECTOR, '#slides tr:nth-child(3)')
        ActionChains(self.driver).drag_and_drop(second,third).perform()

        time.sleep(0.1) # The API that modifies the database runs async
        names=self.session.sessionpresentation_set.values_list('document__name',flat=True) 
        self.assertEqual(list(names),['one','three','two'])


@ifSeleniumEnabled
class AgendaTests(IetfSeleniumTestCase):
    def setUp(self):
        super(AgendaTests, self).setUp()
        self.meeting = make_meeting_test_data()

    def row_id_for_item(self, item):
        return 'row-%s' % item.slug()

    def get_expected_items(self):
        expected_items = self.meeting.schedule.assignments.exclude(timeslot__type__in=['lead','offagenda'])
        self.assertGreater(len(expected_items), 0, 'Test setup generated an empty schedule')
        return expected_items

    def test_agenda_view_js_func_parse_query_params(self):
        """Test parse_query_params() function"""
        self.driver.get(self.absreverse('ietf.meeting.views.agenda'))

        parse_query_params = 'return agenda_filter_for_testing.parse_query_params'

        # Only 'show' param
        result = self.driver.execute_script(
            parse_query_params + '("?show=group1,group2,group3");'
        )
        self.assertEqual(result, dict(show='group1,group2,group3'))

        # Only 'hide' param
        result = self.driver.execute_script(
            parse_query_params + '("?hide=group4,group5,group6");'
        )
        self.assertEqual(result, dict(hide='group4,group5,group6'))

        # Both 'show' and 'hide'
        result = self.driver.execute_script(
            parse_query_params + '("?show=group1,group2,group3&hide=group4,group5,group6");'
        )
        self.assertEqual(result, dict(show='group1,group2,group3', hide='group4,group5,group6'))

        # Encoded
        result = self.driver.execute_script(
            parse_query_params + '("?show=%20group1,%20group2,%20group3&hide=group4,group5,group6");'
        )
        self.assertEqual(result, dict(show=' group1, group2, group3', hide='group4,group5,group6'))

    def test_agenda_view_js_func_toggle_list_item(self):
        """Test toggle_list_item() function"""
        self.driver.get(self.absreverse('ietf.meeting.views.agenda'))

        result = self.driver.execute_script(
            """
            // start empty, add item
            var list0=[];
            %(toggle_list_item)s(list0, 'item');
            
            // one item, remove it
            var list1=['item'];
            %(toggle_list_item)s(list1, 'item');
            
            // one item, add another
            var list2=['item1'];
            %(toggle_list_item)s(list2, 'item2');
            
            // multiple items, remove first
            var list3=['item1', 'item2', 'item3'];
            %(toggle_list_item)s(list3, 'item1');
            
            // multiple items, remove middle
            var list4=['item1', 'item2', 'item3'];
            %(toggle_list_item)s(list4, 'item2');
            
            // multiple items, remove last
            var list5=['item1', 'item2', 'item3'];
            %(toggle_list_item)s(list5, 'item3');
            
            return [list0, list1, list2, list3, list4, list5];
            """ % {'toggle_list_item': 'agenda_filter_for_testing.toggle_list_item'}
        )
        self.assertEqual(result[0], ['item'], 'Adding item to empty list failed')
        self.assertEqual(result[1], [], 'Removing only item in a list failed')
        self.assertEqual(result[2], ['item1', 'item2'], 'Adding second item to list failed')
        self.assertEqual(result[3], ['item2', 'item3'], 'Removing first item from list failed')
        self.assertEqual(result[4], ['item1', 'item3'], 'Removing middle item from list failed')
        self.assertEqual(result[5], ['item1', 'item2'], 'Removing last item from list failed')

    def do_agenda_view_filter_test(self, querystring, visible_groups=()):
        self.login()
        self.driver.get(self.absreverse('ietf.meeting.views.agenda') + querystring)
        self.assert_agenda_item_visibility(visible_groups)
        self.assert_agenda_view_filter_matches_ics_filter(querystring)
        weekview_iframe = self.driver.find_element(By.CSS_SELECTOR, '#weekview iframe')
        if len(querystring) == 0:
            self.assertFalse(weekview_iframe.is_displayed(), 'Weekview should be hidden when filters off')
        else:
            self.assertTrue(weekview_iframe.is_displayed(), 'Weekview should be visible when filters on')
            self.driver.switch_to.frame(weekview_iframe)
            self.assert_weekview_item_visibility(visible_groups)
            self.driver.switch_to.default_content()

    def test_agenda_view_filter_default(self):
        """Filtered agenda view should display only matching rows (all groups selected)"""
        self.do_agenda_view_filter_test('', None)  # None means all should be visible

    def test_agenda_view_filter_show_group(self):
        self.do_agenda_view_filter_test('?show=', [])
        self.do_agenda_view_filter_test('?show=mars', ['mars'])
        self.do_agenda_view_filter_test('?show=mars,ames', ['mars', 'ames'])

    def test_agenda_view_filter_show_area(self):
        mars = Group.objects.get(acronym='mars')
        area = mars.parent
        self.do_agenda_view_filter_test('?show=%s' % area.acronym, ['ames', 'mars'])

    def test_agenda_view_filter_show_type(self):
        self.do_agenda_view_filter_test('?show=reg,break', ['secretariat'])

    def test_agenda_view_filter_show_bof(self):
        mars = Group.objects.get(acronym='mars')
        mars.state_id = 'bof'
        mars.save()
        self.do_agenda_view_filter_test('?show=bof', ['mars'])
        self.do_agenda_view_filter_test('?show=bof,ames', ['ames', 'mars'])

    def test_agenda_view_filter_show_ad_office_hours(self):
        area = GroupFactory(type_id='area')
        SessionFactory(
            meeting__type_id='ietf', 
            type_id='other',
            group=area,
            name='%s Office Hours' % area.acronym,
        )
        self.do_agenda_view_filter_test('?show=adofficehours', [area.acronym])

    def test_agenda_view_filter_hide_group(self):
        mars = Group.objects.get(acronym='mars')
        mars.state_id = 'bof'
        mars.save()
        area = mars.parent

        # Nothing shown, nothing visible
        self.do_agenda_view_filter_test('?hide=mars', [])
        
        # Group shown
        self.do_agenda_view_filter_test('?show=ames,mars&hide=mars', ['ames'])
        
        # Area shown
        self.do_agenda_view_filter_test('?show=%s&hide=mars' % area.acronym, ['ames'])

        # Type shown
        self.do_agenda_view_filter_test('?show=plenary,regular&hide=mars', ['ames','ietf'])

        # bof shown
        self.do_agenda_view_filter_test('?show=bof&hide=mars', [])


    def test_agenda_view_filter_hide_area(self):
        mars = Group.objects.get(acronym='mars')
        mars.state_id = 'bof'
        mars.save()
        area = mars.parent
        SessionFactory(
            meeting__type_id='ietf',
            type_id='other',
            group=area,
            name='%s Office Hours' % area.acronym,
        )

        # Nothing shown
        self.do_agenda_view_filter_test('?hide=%s' % area.acronym, [])
        
        # Group shown
        self.do_agenda_view_filter_test('?show=ames,mars&hide=%s' % area.acronym, [])
        
        # Area shown
        self.do_agenda_view_filter_test('?show=%s&hide=%s' % (area.acronym, area.acronym), [])

        # Type shown
        self.do_agenda_view_filter_test('?show=plenary,regular&hide=%s' % area.acronym, ['ietf'])

        # bof shown
        self.do_agenda_view_filter_test('?show=bof&hide=%s' % area.acronym, [])
        
        # AD office hours shown
        self.do_agenda_view_filter_test('?show=adofficehours&hide=%s' % area.acronym, [])

    def test_agenda_view_filter_hide_type(self):
        mars = Group.objects.get(acronym='mars')
        mars.state_id = 'bof'
        mars.save()
        area = mars.parent
        SessionFactory(
            meeting__type_id='ietf',
            type_id='other',
            group=area,
            name='%s Office Hours' % area.acronym,
        )

        # Nothing shown
        self.do_agenda_view_filter_test('?hide=plenary', [])

        # Group shown
        self.do_agenda_view_filter_test('?show=ietf,ames&hide=plenary', ['ames'])

        # Area shown
        self.do_agenda_view_filter_test('?show=%s&hide=regular' % area.acronym, [])

        # Type shown
        self.do_agenda_view_filter_test('?show=plenary,regular&hide=plenary', ['ames', 'mars'])

        # bof shown
        self.do_agenda_view_filter_test('?show=bof&hide=regular', [])

        # AD office hours shown
        self.do_agenda_view_filter_test('?show=adofficehours&hide=other', [])

    def test_agenda_view_filter_hide_bof(self):
        mars = Group.objects.get(acronym='mars')
        mars.state_id = 'bof'
        mars.save()
        area = mars.parent

        # Nothing shown
        self.do_agenda_view_filter_test('?hide=bof', [])

        # Group shown
        self.do_agenda_view_filter_test('?show=mars,ames&hide=bof', ['ames'])

        # Area shown
        self.do_agenda_view_filter_test('?show=%s&hide=bof' % area.acronym, ['ames'])

        # Type shown
        self.do_agenda_view_filter_test('?show=regular&hide=bof', ['ames'])

        # bof shown
        self.do_agenda_view_filter_test('?show=bof&hide=bof', [])

    def test_agenda_view_filter_hide_ad_office_hours(self):
        mars = Group.objects.get(acronym='mars')
        mars.state_id = 'bof'
        mars.save()
        area = mars.parent
        SessionFactory(
            meeting__type_id='ietf',
            type_id='other',
            group=area,
            name='%s Office Hours' % area.acronym,
        )

        # Nothing shown
        self.do_agenda_view_filter_test('?hide=adofficehours', [])

        # Area shown
        self.do_agenda_view_filter_test('?show=%s&hide=adofficehours' % area.acronym, ['ames', 'mars'])

        # Type shown
        self.do_agenda_view_filter_test('?show=plenary,other&hide=adofficehours', ['ietf'])

        # AD office hours shown
        self.do_agenda_view_filter_test('?show=adofficehours&hide=adofficehours', [])

    def test_agenda_view_filter_whitespace(self):
        self.do_agenda_view_filter_test('?show=  ames , mars &hide=  mars ', ['ames'])

    def assert_agenda_item_visibility(self, visible_groups=None):
        """Assert that correct items are visible in current browser window
        
        If visible_groups is None (the default), expects all items to be visible.
        """
        for item in self.get_expected_items():
            row_id = self.row_id_for_item(item)
            try:
                item_row = self.driver.find_element(By.ID, row_id)
            except NoSuchElementException:
                item_row = None
            self.assertIsNotNone(item_row, 'No row for schedule item "%s"' % row_id)
            if visible_groups is None or item.session.group.acronym in visible_groups:
                self.assertTrue(item_row.is_displayed(), 'Row for schedule item "%s" is not displayed but should be' % row_id)
            else:
                self.assertFalse(item_row.is_displayed(), 'Row for schedule item "%s" is displayed but should not be' % row_id)

    def assert_weekview_item_visibility(self, visible_groups=None):
        for item in self.get_expected_items():
            if item.session.name:
                label = item.session.name
            elif item.slot_type().slug == 'break':
                label = item.timeslot.name
            elif item.session.group:
                label = item.session.group.name
            else:
                label = 'Free Slot'

            try:
                item_div = self.driver.find_element(By.XPATH, '//div/span[contains(text(),"%s")]/..' % label)
            except NoSuchElementException:
                item_div = None

            if visible_groups is None or item.session.group.acronym in visible_groups:
                self.assertIsNotNone(item_div, 'No weekview entry for "%s" (%s)' % (label, item.slug()))
                self.assertTrue(item_div.is_displayed(), 'Entry for "%s (%s)" is not displayed but should be' % (label, item.slug()))
            else:
                self.assertIsNone(item_div, 'Unexpected weekview entry for "%s" (%s)' % (label, item.slug()))

    @staticmethod
    def open_agenda_filter_ui(wait):
        """Click the 'customize' anchor to reveal the group buttons"""
        customize_anchor = wait.until(
            expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, '#accordion button[data-bs-toggle="collapse"]')
            )
        )
        customize_anchor.click()
        return customize_anchor

    @staticmethod
    def get_agenda_filter_group_button(wait, group_acronym):
        return wait.until(
            expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, 'button.pickview.%s' % group_acronym)
            )
        )

    def test_agenda_timeslot_label_visibility(self):
        """The timeslot label for regular sessions should only be shown when a session is visible"""
        wait = WebDriverWait(self.driver, 2)
        url = self.absreverse('ietf.meeting.views.agenda')
        mars_assignments = self.meeting.schedule.assignments.filter(session__group__acronym='mars')
        ames_assignments = self.meeting.schedule.assignments.filter(session__group__acronym='ames')
        assert(mars_assignments.count() == 1)  # if not, need to update test
        assert(ames_assignments.count() == 1)  # if not, need to update test
        assignments = dict(
            mars=mars_assignments.first(),
            ames=ames_assignments.first(),
        )
        # test relies on these timeslots being different so they will have separate label rows
        assert(assignments['mars'].timeslot.time != assignments['ames'].timeslot.time)
        label_row_selectors = {
            grp: (By.CSS_SELECTOR, 'tr.session-label-row[data-slot-start-ts="{}"][data-slot-end-ts="{}"]'.format(
                int(assignment.timeslot.utc_start_time().timestamp()),
                int(assignment.timeslot.utc_end_time().timestamp()),
            ))
            for grp, assignment in assignments.items()
        }

        self.login()

        # get page with all items visible
        self.driver.get(url)
        wait.until(expected_conditions.visibility_of_element_located(label_row_selectors['ames']))
        wait.until(expected_conditions.visibility_of_element_located(label_row_selectors['mars']))

        # get page with ames hidden
        self.driver.get(url + '?show=mars&hide=ames')
        wait.until(expected_conditions.invisibility_of_element_located(label_row_selectors['ames']))
        wait.until(expected_conditions.visibility_of_element_located(label_row_selectors['mars']))

        # get page with mars hidden
        self.driver.get(url + '?show=ames&hide=mars')
        wait.until(expected_conditions.visibility_of_element_located(label_row_selectors['ames']))
        wait.until(expected_conditions.invisibility_of_element_located(label_row_selectors['mars']))

        # create an ames session in the mars timeslot, should cause the mars timeslot label to reappear
        sess = SessionFactory(group=Group.objects.get(acronym='ames'),
                              meeting=self.meeting,
                              add_to_schedule=False)
        sess.timeslotassignments.create(timeslot=assignments['mars'].timeslot,
                                        schedule=self.meeting.schedule)
        self.driver.get(url + '?show=ames&hide=mars')
        wait.until(expected_conditions.visibility_of_element_located(label_row_selectors['ames']))
        wait.until(expected_conditions.visibility_of_element_located(label_row_selectors['mars']))

        # get page with ames and mars hidden
        self.driver.get(url + '?hide=ames,mars')
        wait.until(expected_conditions.invisibility_of_element_located(label_row_selectors['ames']))
        wait.until(expected_conditions.invisibility_of_element_located(label_row_selectors['mars']))

    def test_agenda_view_group_filter_toggle(self):
        """Clicking a group toggle enables/disables agenda filtering"""
        wait = WebDriverWait(self.driver, 2)
        group_acronym = 'mars'

        self.login()
        url = self.absreverse('ietf.meeting.views.agenda')
        self.driver.get(url)
        
        self.open_agenda_filter_ui(wait)
        
        # Click the group button
        group_button = self.get_agenda_filter_group_button(wait, group_acronym)
        self.scroll_to_element(group_button)
        group_button.click()

        # Check visibility
        self.assert_agenda_item_visibility([group_acronym])
        
        # Click the group button again
        group_button = self.get_agenda_filter_group_button(wait, group_acronym)
        group_button.click()

        # Check visibility
        self.assert_agenda_item_visibility()

    def test_agenda_view_team_group_filter_toggle(self):
        """'Team' group sessions should not respond to area filter button

        Sessions belonging to 'team' groups should not respond to their parent buttons. This prevents,
        e.g., 'hackathon', or 'tools' group sessions from being shown/hidden when their parent group
        filter button is clicked. 
        """
        def _schedule_session(meeting, session):
            """Schedule a session, guaranteeing that it is not in a private timeslot"""
            SchedTimeSessAssignment.objects.create(
                schedule=meeting.schedule,
                timeslot=TimeSlotFactory(meeting=meeting),
                session=session,
            )

        wait = WebDriverWait(self.driver, 10)
        meeting = Meeting.objects.get(type_id='ietf')
        parent_group = GroupFactory(type_id='area')
        other_group = GroupFactory(parent=parent_group, type_id='wg')
        hackathon_group = GroupFactory(acronym='hackathon', type_id='team', parent=parent_group)

        # hackathon session
        #
        # Add to schedule ourselves because the default scheduling sometimes puts the session
        # in a private timeslot, preventing the session from appearing on the agenda and breaking
        # the test.
        _schedule_session(
            meeting,
            SessionFactory(
                meeting=meeting,
                type_id='other',
                group=hackathon_group,
                name='Hackathon',
                add_to_schedule=False
            )
        )

        # Session to cause the parent_group to appear in the filter UI tables.
        _schedule_session(
            meeting,
            SessionFactory(meeting=meeting, type_id='regular', group=other_group, add_to_schedule=False)
        )

        self.login()
        url = self.absreverse('ietf.meeting.views.agenda')
        self.driver.get(url)

        self.open_agenda_filter_ui(wait)

        self.get_agenda_filter_group_button(wait, 'mars').click()
        self.assert_agenda_item_visibility(['mars'])

        # enable hackathon group
        group_button = self.get_agenda_filter_group_button(wait, 'hackathon')
        self.scroll_to_element(group_button)
        group_button.click()
        self.assert_agenda_item_visibility(['mars', 'hackathon'])

        # disable hackathon group
        group_button.click()
        self.assert_agenda_item_visibility(['mars'])

        # clicking area should not show the hackathon
        self.get_agenda_filter_group_button(wait, parent_group.acronym).click()
        self.assert_agenda_item_visibility(['mars', other_group.acronym])

    def test_agenda_view_group_filter_toggle_without_replace_state(self):
        """Toggle should function for browsers without window.history.replaceState"""
        group_acronym = 'mars'

        self.login()
        url = self.absreverse('ietf.meeting.views.agenda')
        self.driver.get(url)
        
        # Rather than digging up an ancient browser, simulate absence of history.replaceState
        self.driver.execute_script('window.history.replaceState = undefined;')

        
        # Click the 'customize' anchor to reveal the group buttons
        customize_anchor = WebDriverWait(self.driver, 2).until(
            expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, '#accordion button[data-bs-toggle="collapse"]')
            )
        )
        customize_anchor.click()

        
        # Get ready to click the group button
        group_button = WebDriverWait(self.driver, 2).until(
            expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, 'button.pickview.%s' % group_acronym)
            )
        )

        # Be sure we're at the URL we think we're at before we click
        self.assertEqual(self.driver.current_url, url)
        self.scroll_to_element(group_button)
        group_button.click()  # click!

        expected_url = '%s?show=%s' % (url, group_acronym)
        WebDriverWait(self.driver, 2).until(expected_conditions.url_to_be(expected_url))
        # no assertion here - if WebDriverWait raises an exception, the test will fail.
        # We separately test whether this URL will filter correctly.

    def session_from_agenda_row_id(self, row_id):
        """Find session corresponding to a row in the agenda HTML"""
        components = row_id.split('-', 8)
        # for regular session:
        #   row-<meeting#>-<year>-<month>-<day>-<DoW>-<HHMM>-<parent acro>-<group acro>
        # for plenary session:
        #   row-<meeting#>-<year>-<month>-<day>-<DoW>-<HHMM>-1plenary-<group acro>
        # for others (break, reg, other):
        #   row-<meeting#>-<year>-<month>-<day>-<DoW>-<HHMM>-<group acro>-<session name slug>
        meeting_number = components[1]
        start_time = datetime.datetime(
            year=int(components[2]),
            month=int(components[3]),
            day=int(components[4]),
            hour=int(components[6][0:2]),
            minute=int(components[6][2:4]),
        )
        # If labeled as plenary, it's plenary...
        if components[7] == '1plenary':
            session_type = 'plenary'
            group = Group.objects.get(acronym=components[8])
        else:
            # If not a plenary, see if the last component is a group
            try:
                group = Group.objects.get(acronym=components[8])
            except Group.DoesNotExist:
                # Last component was not a group, so this must not be a regular session
                session_type = 'other'
                group = Group.objects.get(acronym=components[7])
            else:
                # Last component was a group, this is a regular session
                session_type = 'regular'
        
        meeting = Meeting.objects.get(number=meeting_number)
        possible_assignments = SchedTimeSessAssignment.objects.filter(
            schedule__in=[meeting.schedule, meeting.schedule.base],
            timeslot__time=start_time,
        )
        if session_type == 'other':
            possible_sessions = [pa.session for pa in possible_assignments.filter(
                timeslot__type_id__in=['break', 'reg', 'other'], session__group=group
            ) if slugify(pa.session.name) == components[8]]
            if len(possible_sessions) != 1:
                raise ValueError('No unique matching session for row %s (found %d)' % (
                    row_id, len(possible_sessions)
                ))
            session = possible_sessions[0]
        else:
            session = possible_assignments.filter(
                timeslot__type_id=session_type
            ).get(session__group=group).session
        return session, possible_assignments.get(session=session).timeslot

    def assert_agenda_view_filter_matches_ics_filter(self, filter_string):
        """The agenda view and ics view should show the same events for a given filter
        
        This must be called after using self.driver.get to load the agenda page
        to be checked.
        """
        ics_url = self.absreverse('ietf.meeting.views.agenda_ical')
        
        # parse out the events
        agenda_rows = self.driver.find_elements(By.CSS_SELECTOR, '[id^="row-"]:not(.info)')
        visible_rows = [r for r in agenda_rows if r.is_displayed()]
        sessions = [self.session_from_agenda_row_id(row.get_attribute("id")) 
                    for row in visible_rows]
        r = self.client.get(ics_url + filter_string)
        # verify that all expected sessions are found
        expected_uids = [
            'ietf-%s-%s-%s' % (session.meeting.number, timeslot.pk, session.group.acronym) 
            for (session, timeslot) in sessions
        ]
        assert_ical_response_is_valid(self, r, 
                                      expected_event_uids=expected_uids,
                                      expected_event_count=len(sessions))

    def test_session_materials_modal(self):
        """Test opening and re-opening a session materials modal

        This currently only tests the slides to ensure that changes to these are picked up
        without reloading the main agenda page. This should also test that the agenda and
        minutes are displayed and updated correctly, but problems with WebDriver/Selenium/Chromedriver
        are blocking this.
        """
        session = self.meeting.session_set.filter(group__acronym="mars").first()
        assignment = session.official_timeslotassignment()
        slug = assignment.slug()

        url = self.absreverse('ietf.meeting.views.agenda')
        self.driver.get(url)

        # modal should start hidden
        modal_div = self.driver.find_element(By.CSS_SELECTOR, 'div#modal-%s' % slug)
        self.assertFalse(modal_div.is_displayed())

        # Click the 'materials' button
        open_modal_button = WebDriverWait(self.driver, 2).until(
            expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, '[data-bs-target="#modal-%s"]' % slug)
            ),
            'Modal open button not found or not clickable',
        )
        self.scroll_to_element(open_modal_button)
        open_modal_button.click()
        WebDriverWait(self.driver, 2).until(
            expected_conditions.visibility_of(modal_div),
            'Modal did not become visible after clicking open button',
        )

        # Check that we have the expected slides
        not_deleted_slides = session.materials.filter(
            type='slides'
        ).exclude(
            states__type__slug='slides',states__slug='deleted'
        )
        self.assertGreater(not_deleted_slides.count(), 0)  # make sure this isn't a pointless test
        for slide in not_deleted_slides:
            anchor = self.driver.find_element(By.XPATH, '//a[text()="%s"]' % slide.title)
            self.assertIsNotNone(anchor)

        deleted_slides = session.materials.filter(
            type='slides', states__type__slug='slides', states__slug='deleted'
        )
        self.assertGreater(deleted_slides.count(), 0)  # make sure this isn't a pointless test
        for slide in deleted_slides:
            with self.assertRaises(NoSuchElementException):
                self.driver.find_element(By.XPATH, '//a[text()="%s"]' % slide.title)

        # Now close the modal
        close_modal_button = WebDriverWait(self.driver, 2).until(
            expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, '.modal-footer button[data-bs-dismiss="modal"]')
            ),
            'Modal close button not found or not clickable',
        )
        self.scroll_to_element(close_modal_button)
        close_modal_button.click()
        WebDriverWait(self.driver, 2).until(
            expected_conditions.invisibility_of_element(modal_div),
            'Modal was not hidden after clicking close button',
        )

        # Modify the session info
        newly_deleted_slide = not_deleted_slides.first()
        newly_undeleted_slide = deleted_slides.first()
        newly_deleted_slide.set_state(State.objects.get(type="slides", slug="deleted"))
        newly_undeleted_slide.set_state(State.objects.get(type="slides", slug="active"))

        # Click the 'materials' button
        open_modal_button = WebDriverWait(self.driver, 2).until(
            expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, '[data-bs-target="#modal-%s"]' % slug)
            ),
            'Modal open button not found or not clickable for refresh test',
        )
        self.scroll_to_element(open_modal_button)
        open_modal_button.click()
        # self.driver.execute_script("arguments[0].click();", open_modal_button)
        WebDriverWait(self.driver, 2).until(
            expected_conditions.visibility_of(modal_div),
            'Modal did not become visible after clicking open button for refresh test',
        )

        # Check that we now see the updated slides
        not_deleted_slides = session.materials.filter(
            type='slides'
        ).exclude(
            states__type__slug='slides',states__slug='deleted'
        )
        self.assertNotIn(newly_deleted_slide, not_deleted_slides)
        self.assertIn(newly_undeleted_slide, not_deleted_slides)
        for slide in not_deleted_slides:
            anchor = self.driver.find_element(By.XPATH, '//a[text()="%s"]' % slide.title)
            self.assertIsNotNone(anchor)

        deleted_slides = session.materials.filter(
            type='slides', states__type__slug='slides', states__slug='deleted'
        )
        self.assertIn(newly_deleted_slide, deleted_slides)
        self.assertNotIn(newly_undeleted_slide, deleted_slides)
        for slide in deleted_slides:
            with self.assertRaises(NoSuchElementException):
                self.driver.find_element(By.XPATH, '//a[text()="%s"]' % slide.title)

    def test_agenda_time_zone_selection(self):
        self.assertNotEqual(self.meeting.time_zone, 'UTC', 'Meeting time zone must not be UTC')

        wait = WebDriverWait(self.driver, 2)
        self.driver.get(self.absreverse('ietf.meeting.views.agenda'))

        # wait for the select box to be updated - look for an arbitrary time zone to be in
        # its options list to detect this
        arbitrary_tz = 'America/Halifax'
        arbitrary_tz_opt = WebDriverWait(self.driver, 2).until(
            expected_conditions.presence_of_element_located(
                (By.CSS_SELECTOR, '#timezone-select > option[value="%s"]' % arbitrary_tz)
            )
        )

        tz_select_input = self.driver.find_element(By.ID, 'timezone-select')
        meeting_tz_link = self.driver.find_element(By.CSS_SELECTOR, 'label[for="meeting-timezone"]')
        local_tz_link = self.driver.find_element(By.CSS_SELECTOR, 'label[for="local-timezone"]')
        utc_tz_link = self.driver.find_element(By.CSS_SELECTOR, 'label[for="utc-timezone"]')
        # we'll check that all tz-select elements are updated, but first check that at least one is in the nav sidebar
        self.assertIsNotNone(self.driver.find_element(By.CSS_SELECTOR, '.tz-select'))

        # Moment.js guesses local time zone based on the behavior of Selenium's web client. This seems
        # to inherit Django's settings.TIME_ZONE but I don't know whether that's guaranteed to be consistent.
        # To avoid test fragility, ask Moment what it considers local and expect that.
        local_tz = self.driver.execute_script('return moment.tz.guess();')
        self.assertNotEqual(self.meeting.time_zone, local_tz, 'Meeting time zone must not be local time zone')
        self.assertNotEqual(local_tz, 'UTC', 'Local time zone must not be UTC')

        meeting_tz_opt = tz_select_input.find_element(By.CSS_SELECTOR, 'option[value="%s"]' % self.meeting.time_zone)
        local_tz_opt = tz_select_input.find_element(By.CSS_SELECTOR, 'option[value="%s"]' % local_tz)
        utc_tz_opt = tz_select_input.find_element(By.CSS_SELECTOR, 'option[value="UTC"]')

        # Should start off in meeting time zone
        self.assertTrue(meeting_tz_opt.is_selected())
        # don't yet know local_tz, so can't check that it's deselected here
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertFalse(utc_tz_opt.is_selected())
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '.tz-select option:checked').text.strip(), self.meeting.time_zone)

        # Click 'local' button
        local_tz_link.click()
        wait.until(expected_conditions.element_selection_state_to_be(meeting_tz_opt, False))
        self.assertFalse(meeting_tz_opt.is_selected())
        # just identified the local_tz_opt as being selected, so no check here, either
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertFalse(utc_tz_opt.is_selected())
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '.tz-select option:checked').text.strip(), local_tz)

        # click 'utc' button
        utc_tz_link.click()
        wait.until(expected_conditions.element_to_be_selected(utc_tz_opt))
        self.assertFalse(meeting_tz_opt.is_selected())
        self.assertFalse(local_tz_opt.is_selected())  # finally!
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertTrue(utc_tz_opt.is_selected())
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '.tz-select option:checked').text.strip(), 'UTC')

        # click back to meeting
        meeting_tz_link.click()
        wait.until(expected_conditions.element_to_be_selected(meeting_tz_opt))
        self.assertTrue(meeting_tz_opt.is_selected())
        self.assertFalse(local_tz_opt.is_selected())
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertFalse(utc_tz_opt.is_selected())
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '.tz-select option:checked').text.strip(), self.meeting.time_zone)

        # and then back to UTC...
        utc_tz_link.click()
        wait.until(expected_conditions.element_to_be_selected(utc_tz_opt))
        self.assertFalse(meeting_tz_opt.is_selected())
        self.assertFalse(local_tz_opt.is_selected())
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertTrue(utc_tz_opt.is_selected())
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '.tz-select option:checked').text.strip(), 'UTC')

        # ... and test the switch from UTC to local
        local_tz_link.click()
        wait.until(expected_conditions.element_to_be_selected(local_tz_opt))
        self.assertFalse(meeting_tz_opt.is_selected())
        self.assertTrue(local_tz_opt.is_selected())
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertFalse(utc_tz_opt.is_selected())
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '.tz-select option:checked').text.strip(), local_tz)

        # Now select a different item from the select input
        arbitrary_tz_opt.click()
        wait.until(expected_conditions.element_to_be_selected(arbitrary_tz_opt))
        self.assertFalse(meeting_tz_opt.is_selected())
        self.assertFalse(local_tz_opt.is_selected())
        self.assertTrue(arbitrary_tz_opt.is_selected())
        self.assertFalse(utc_tz_opt.is_selected())
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '.tz-select option:checked').text.strip(), arbitrary_tz)
    
    def test_agenda_time_zone_selection_updates_weekview(self):
        """Changing the time zone should update the weekview to match"""
        class in_iframe_href:
            """Condition class for use with WebDriverWait"""
            def __init__(self, fragment, iframe):
                self.fragment = fragment
                self.iframe = iframe

            def __call__(self, driver):
                driver.switch_to.frame(self.iframe)
                current_href= driver.execute_script(
                    'return document.location.href'
                )
                driver.switch_to.default_content()
                return self.fragment in current_href
            
        # enable a filter so the weekview iframe is visible
        self.driver.get(self.absreverse('ietf.meeting.views.agenda') + '?show=mars')
        # wait for the select box to be updated - look for an arbitrary time zone to be in
        # its options list to detect this
        wait = WebDriverWait(self.driver, 2)
        option = wait.until(
            expected_conditions.presence_of_element_located(
                (By.CSS_SELECTOR, '#timezone-select > option[value="America/Halifax"]'))
        )
        # Now select a different item from the select input
        option.click()
        try:
            wait.until(in_iframe_href(urllib.parse.quote('tz=america/halifax', safe='='), self.driver.find_element(By.CSS_SELECTOR, '#weekview iframe')))
        except:
            self.fail('iframe href not updated to contain selected time zone')

    def test_agenda_session_selection(self):
        # create a second mars session to test selection of specific sessions
        SessionFactory(
            meeting=self.meeting,
            group__acronym='mars',
            add_to_schedule=False,
        ).timeslotassignments.create(
            timeslot=TimeSlotFactory(meeting=self.meeting, duration=datetime.timedelta(minutes=60)),
            schedule=self.meeting.schedule,
        )

        wait = WebDriverWait(self.driver, 2)
        url = self.absreverse('ietf.meeting.views.agenda_personalize', kwargs={'num': self.meeting.number})
        self.driver.get(url)

        # Verify that elements are all updated when the filters change. That the correct elements
        # have the appropriate classes is a separate test.
        elements_to_check = self.driver.find_elements(By.CSS_SELECTOR, '.agenda-link.filterable')
        self.assertGreater(len(elements_to_check), 0, 'No elements with agenda links to update were found')

        self.assertFalse(
            any(checkbox.is_selected()
                for checkbox in self.driver.find_elements(By.CSS_SELECTOR,
                'input.checkbox[name="selected-sessions"]')),
            'Sessions were selected before being clicked',
        )

        mars_sessa_checkbox = self.driver.find_element(By.CSS_SELECTOR, 'input[type="checkbox"][name="selected-sessions"][data-filter-item="mars-sessa"]')
        mars_sessb_checkbox = self.driver.find_element(By.CSS_SELECTOR, 'input[type="checkbox"][name="selected-sessions"][data-filter-item="mars-sessb"]')
        farfut_button = self.driver.find_element(By.CSS_SELECTOR, 'button[data-filter-item="farfut"]')
        break_checkbox = self.driver.find_element(By.CSS_SELECTOR, 'input[type="checkbox"][name="selected-sessions"][data-filter-item="secretariat-sessb"]')
        registration_checkbox = self.driver.find_element(By.CSS_SELECTOR, 'input[type="checkbox"][name="selected-sessions"][data-filter-item="secretariat-sessa"]')
        self.driver.execute_script("arguments[0].click();", mars_sessa_checkbox)  # select mars session; FIXME: no idea why a simple mars_sessa_checkbox.click() doesn't work

        try:
            wait.until(
                lambda driver: all('?show=mars-sessa' in el.get_attribute('href') for el in elements_to_check)
            )
        except TimeoutException:
            self.fail('Some agenda links were not updated when mars session was selected')
        self.assertTrue(mars_sessa_checkbox.is_selected(), 'mars session A checkbox was not selected after being clicked')
        self.assertFalse(mars_sessb_checkbox.is_selected(), 'mars session B checkbox was selected without being clicked')
        self.assertFalse(break_checkbox.is_selected(), 'break checkbox was selected without being clicked')
        self.assertFalse(registration_checkbox.is_selected(), 'registration checkbox was selected without being clicked')

        self.driver.execute_script("arguments[0].click();", mars_sessa_checkbox)  # deselect mars session
        try:
            wait.until(
                lambda driver: not any('?show=mars-sessa' in el.get_attribute('href') for el in elements_to_check)
            )
        except TimeoutException:
            self.fail('Some agenda links were not updated when mars session was de-selected')
        self.assertFalse(mars_sessa_checkbox.is_selected(), 'mars session A checkbox was still selected after being clicked')
        self.assertFalse(mars_sessb_checkbox.is_selected(), 'mars session B checkbox was selected without being clicked')
        self.assertFalse(break_checkbox.is_selected(), 'break checkbox was selected without being clicked')
        self.assertFalse(registration_checkbox.is_selected(), 'registration checkbox was selected without being clicked')

        farfut_button.click()  # turn on all farfut area sessions
        self.driver.execute_script("arguments[0].click();", mars_sessa_checkbox)  # but turn off mars session a
        self.driver.execute_script("arguments[0].click();", break_checkbox)  # also select the break

        try:
            wait.until(
                lambda driver: all(
                    '?show=farfut,secretariat-sessb&hide=mars-sessa' in el.get_attribute('href')
                    for el in elements_to_check
                ))
        except TimeoutException:
            self.fail('Some agenda links were not updated when farfut area was selected')
        self.assertFalse(mars_sessa_checkbox.is_selected(), 'mars session A checkbox was unexpectedly selected')
        self.assertTrue(mars_sessb_checkbox.is_selected(), 'mars session B checkbox was expected to be selected')
        self.assertTrue(break_checkbox.is_selected(), 'break checkbox was expected to be selected')
        self.assertFalse(registration_checkbox.is_selected(), 'registration checkbox was unexpectedly selected')

@ifSeleniumEnabled
class WeekviewTests(IetfSeleniumTestCase):
    def setUp(self):
        super(WeekviewTests, self).setUp()
        self.meeting = make_meeting_test_data()

    def get_expected_items(self):
        expected_items = self.meeting.schedule.assignments.exclude(timeslot__type__in=['lead', 'offagenda'])
        self.assertGreater(len(expected_items), 0, 'Test setup generated an empty schedule')
        return expected_items

    def test_timezone_default(self):
        """Week view should show UTC times by default"""
        self.assertNotEqual(self.meeting.time_zone.lower(), 'utc',
                            'Cannot test local time weekview because meeting is using UTC time.')
        self.login()
        self.driver.get(self.absreverse('ietf.meeting.views.week_view'))
        for item in self.get_expected_items():
            if item.session.name:
                expected_name = item.session.name
            elif item.slot_type().slug == 'break':
                expected_name = item.timeslot.name
            else:
                expected_name = item.session.group.name
            expected_time = '-'.join([item.timeslot.utc_start_time().strftime('%H%M'),
                                      item.timeslot.utc_end_time().strftime('%H%M')])
            WebDriverWait(self.driver, 2).until(
                expected_conditions.presence_of_element_located(
                    (By.XPATH, 
                     '//div/div[contains(text(), "%s")]/span[contains(text(), "%s")]' % (
                         expected_time, expected_name))
                )
            )

    def test_timezone_selection(self):
        """Week view should show time zones when requested"""
        # Must test utc; others are picked arbitrarily
        zones_to_test = ['utc', 'America/Halifax', 'Asia/Bangkok', 'Africa/Dakar', 'Europe/Dublin']
        self.login()
        for zone_name in zones_to_test:
            zone = pytz.timezone(zone_name)
            self.driver.get(self.absreverse('ietf.meeting.views.week_view') + '?tz=' + urllib.parse.quote(zone_name, safe=''))
            for item in self.get_expected_items():
                if item.session.name:
                    expected_name = item.session.name
                elif item.slot_type().slug == 'break':
                    expected_name = item.timeslot.name
                else:
                    expected_name = item.session.group.name

                start_time = item.timeslot.utc_start_time().astimezone(zone)
                end_time = item.timeslot.utc_end_time().astimezone(zone)
                expected_time = '-'.join([start_time.strftime('%H%M'),
                                          end_time.strftime('%H%M')])

                WebDriverWait(self.driver, 2).until(
                    expected_conditions.presence_of_element_located(
                        (By.XPATH, 
                         '//div/div[contains(text(), "%s")]/span[contains(text(), "%s")]' % (
                             expected_time, expected_name))
                    ),
                    'Could not find event "%s" at %s for time zone %s' % (expected_name, 
                                                                          expected_time,
                                                                          zone_name),
                )

    def test_event_wrapping(self):
        """Events that overlap midnight should be shown on both days
        
        This assumes that the meeting is in US/Eastern timezone.
        """
        def _assert_wrapped(displayed, expected_time_string):
            self.assertEqual(len(displayed), 2)
            first = displayed[0]
            first_parent = first.find_element(By.XPATH, '..')
            second = displayed[1]
            second_parent = second.find_element(By.XPATH, '..')
            self.assertNotIn('continued', first.text)
            self.assertIn(expected_time_string, first_parent.text)
            self.assertIn('continued', second.text)
            self.assertIn(expected_time_string, second_parent.text)

        def _assert_not_wrapped(displayed, expected_time_string):
            self.assertEqual(len(displayed), 1)
            first = displayed[0]
            first_parent = first.find_element(By.XPATH, '..')
            self.assertNotIn('continued', first.text)
            self.assertIn(expected_time_string, first_parent.text)

        duration = datetime.timedelta(minutes=120)  # minutes
        local_tz = self.meeting.time_zone
        self.assertEqual(local_tz.lower(), 'us/eastern',
                         'Test logic error - meeting local time zone must be US/Eastern')

        # Session during a single day in meeting local time but multi-day UTC
        # Compute a time that overlaps midnight, UTC, but won't when shifted to a local time zone
        start_time_utc = pytz.timezone('UTC').localize(
            datetime.datetime.combine(self.meeting.date, datetime.time(23,0))
        )
        start_time_local = start_time_utc.astimezone(pytz.timezone(self.meeting.time_zone))

        daytime_session = SessionFactory(
            meeting=self.meeting,
            name='Single Day Session for Wrapping Test',
            add_to_schedule=False,
        )
        daytime_timeslot = TimeSlotFactory(
            meeting=self.meeting,
            time=start_time_local.replace(tzinfo=None),  # drop timezone for Django
            duration=duration,
        )
        daytime_session.timeslotassignments.create(timeslot=daytime_timeslot, schedule=self.meeting.schedule)

        # Session that overlaps midnight in meeting local time
        overnight_session = SessionFactory(
            meeting=self.meeting,
            name='Overnight Session for Wrapping Test',
            add_to_schedule=False,
        )
        overnight_timeslot = TimeSlotFactory(
            meeting=self.meeting,
            time=datetime.datetime.combine(self.meeting.date, datetime.time(23,0)),
            duration=duration,
        )
        overnight_session.timeslotassignments.create(timeslot=overnight_timeslot, schedule=self.meeting.schedule)

        # Check assumptions about events overlapping midnight
        self.assertEqual(daytime_timeslot.local_start_time().day,
                         daytime_timeslot.local_end_time().day,
                         'Daytime event should not overlap midnight in local time')
        self.assertNotEqual(daytime_timeslot.utc_start_time().day,
                           daytime_timeslot.utc_end_time().day,
                           'Daytime event should overlap midnight in UTC')

        self.assertNotEqual(overnight_timeslot.local_start_time().day,
                            overnight_timeslot.local_end_time().day,
                            'Overnight event should overlap midnight in local time')
        self.assertEqual(overnight_timeslot.utc_start_time().day,
                         overnight_timeslot.utc_end_time().day,
                         'Overnight event should not overlap midnight in UTC')

        self.login()
        
        # Test in meeting local time
        self.driver.get(self.absreverse('ietf.meeting.views.week_view') + '?tz=%s' % urllib.parse.quote(local_tz.lower(), safe=''))

        time_string = '-'.join([daytime_timeslot.local_start_time().strftime('%H%M'),
                                daytime_timeslot.local_end_time().strftime('%H%M')])
        displayed = WebDriverWait(self.driver, 2).until(
            expected_conditions.presence_of_all_elements_located(
                (By.XPATH,
                 '//div/div[contains(text(), "%s")]/span[contains(text(), "%s")]' % (
                     time_string,
                     daytime_session.name))
            )
        )
        _assert_not_wrapped(displayed, time_string)

        time_string = '-'.join([overnight_timeslot.local_start_time().strftime('%H%M'),
                                overnight_timeslot.local_end_time().strftime('%H%M')])
        displayed = WebDriverWait(self.driver, 2).until(
            expected_conditions.presence_of_all_elements_located(
                (By.XPATH,
                 '//div/div[contains(text(), "%s")]/span[contains(text(), "%s")]' % (
                     time_string,
                     overnight_session.name))
            )
        )
        _assert_wrapped(displayed, time_string)

        # Test in utc time
        self.driver.get(self.absreverse('ietf.meeting.views.week_view') + '?tz=utc')

        time_string = '-'.join([daytime_timeslot.utc_start_time().strftime('%H%M'),
                                daytime_timeslot.utc_end_time().strftime('%H%M')])
        displayed = WebDriverWait(self.driver, 2).until(
            expected_conditions.presence_of_all_elements_located(
                (By.XPATH,
                 '//div/div[contains(text(), "%s")]/span[contains(text(), "%s")]' % (
                     time_string,
                     daytime_session.name))
            )
        )
        _assert_wrapped(displayed, time_string)

        time_string = '-'.join([overnight_timeslot.utc_start_time().strftime('%H%M'),
                                overnight_timeslot.utc_end_time().strftime('%H%M')])
        displayed = WebDriverWait(self.driver, 2).until(
            expected_conditions.presence_of_all_elements_located(
                (By.XPATH,
                 '//div/div[contains(text(), "%s")]/span[contains(text(), "%s")]' % (
                     time_string,
                     overnight_session.name))
            )
        )
        _assert_not_wrapped(displayed, time_string)

@ifSeleniumEnabled
class InterimTests(IetfSeleniumTestCase):
    def setUp(self):
        super(InterimTests, self).setUp()
        self.materials_dir = self.tempdir('materials')
        self.saved_agenda_path = settings.AGENDA_PATH
        settings.AGENDA_PATH = self.materials_dir
        self.meeting = make_meeting_test_data(create_interims=True)

        # Create a group with a plenary interim session for testing type filters
        somegroup = GroupFactory(acronym='sg', name='Some Group')
        sg_interim = make_interim_meeting(somegroup, datetime.date.today() + datetime.timedelta(days=20))
        sg_sess = sg_interim.session_set.first()
        sg_slot = sg_sess.timeslotassignments.first().timeslot
        sg_sess.purpose_id = 'plenary'
        sg_sess.type_id = 'plenary'
        sg_slot.type_id = 'plenary'
        sg_sess.save()
        sg_slot.save()

        self.wait = WebDriverWait(self.driver, 2)

    def tearDown(self):
        settings.AGENDA_PATH = self.saved_agenda_path
        shutil.rmtree(self.materials_dir)
        super(InterimTests, self).tearDown()

    def tempdir(self, label):
        # Borrowed from  test_utils.TestCase
        slug = slugify(self.__class__.__name__.replace('.','-'))
        dirname = "tmp-{label}-{slug}-dir".format(**locals())
        if 'VIRTUAL_ENV' in os.environ:
            dirname = os.path.join(os.environ['VIRTUAL_ENV'], dirname)
        path = os.path.abspath(dirname)
        if not os.path.exists(path):
            os.mkdir(path)
        return path

    def displayed_interims(self, groups=None):
        sessions = add_event_info_to_session_qs(
            Session.objects.filter(
                meeting__type_id='interim',
                timeslotassignments__schedule=F('meeting__schedule'),
                timeslotassignments__timeslot__time__gte=datetime.datetime.today()
            )
        ).filter(current_status__in=('sched','canceled'))
        meetings = []
        for s in sessions:
            if groups is None or s.group.acronym in groups:
                s.meeting.calendar_label = s.group.acronym  # annotate with group
                meetings.append(s.meeting)
        return meetings

    def all_ietf_meetings(self):
        meetings = Meeting.objects.filter(
            type_id='ietf',
            date__gte=datetime.datetime.today()-datetime.timedelta(days=7)
        )
        for m in meetings:
            m.calendar_label = 'IETF %s' % m.number
        return meetings

    def find_upcoming_meeting_entries(self):
        return self.driver.find_elements(By.CSS_SELECTOR,
            'table#upcoming-meeting-table a.ietf-meeting-link, table#upcoming-meeting-table a.interim-meeting-link'
        )

    def assert_upcoming_meeting_visibility(self, visible_meetings=None):
        """Assert that correct items are visible in current browser window

        If visible_meetings is None (the default), expects all items to be visible.
        """
        expected = {mtg.number for mtg in visible_meetings}
        not_visible = set()
        unexpected = set()
        entries = self.find_upcoming_meeting_entries()
        for entry in entries:
            entry_text = entry.get_attribute('innerHTML').strip()  # gets text, even if element is hidden
            nums = [n for n in expected if n in entry_text]
            self.assertLessEqual(len(nums), 1, 'Multiple matching meeting numbers')
            if len(nums) > 0:  # asserted that it's at most 1, so if it's not 0, it's 1.
                expected.remove(nums[0])
                if not entry.is_displayed():
                    not_visible.add(nums[0])
                continue
            # Found an unexpected row - this is only a problem if it is visible
            if entry.is_displayed():
                unexpected.add(entry_text)

        self.assertEqual(expected, set(), "Missing entries for expected iterim meetings.")
        self.assertEqual(not_visible, set(), "Hidden rows for expected interim meetings.")
        self.assertEqual(unexpected, set(), "Unexpected row visible")

    def assert_upcoming_meeting_calendar(self, visible_meetings=None):
        """Assert that correct items are sent to the calendar"""
        def advance_month():
            button = self.wait.until(
                expected_conditions.element_to_be_clickable(
                    (By.CSS_SELECTOR, 'div#calendar button.fc-next-button')))
            self.driver.execute_script("arguments[0].click();", button)  # FIXME-LARS: no idea why this fails:
            # self.scroll_to_element(button)
            # button.click()

        seen = set()
        not_visible = set()
        unexpected = set()

        # Test that we see all the expected meetings when we scroll through the
        # entire year. We only check the group names / IETF numbers. This should
        # be good enough to catch filtering errors but does not validate the
        # details of what's shown on the calendar. Need 13 iterations instead of
        # 12 in order to check the starting month of the following year, which
        # will usually contain the day 1 year from the start date.
        for _ in range(13):
            entries = self.driver.find_elements(By.CSS_SELECTOR,
                'div#calendar div.fc-event-main'
            )
            for entry in entries:
                meetings = [m for m in visible_meetings if m.calendar_label in entry.text]
                if len(meetings) > 0:
                    seen.add(meetings[0])
                    if not entry.is_displayed():
                        not_visible.add(meetings[0])
                    continue
                # Found an unexpected row - this is ok as long as it's hidden
                if entry.is_displayed():
                    unexpected.add(entry.text)
            advance_month()

        self.assertEqual(seen, visible_meetings, "Expected calendar entries not shown.")
        self.assertEqual(not_visible, set(), "Hidden calendar entries for expected interim meetings.")
        self.assertEqual(unexpected, set(), "Unexpected calendar entries visible")

    def do_upcoming_view_filter_test(self, querystring, visible_meetings=()):
        self.login()
        self.driver.get(self.absreverse('ietf.meeting.views.upcoming') + querystring)
        self.assert_upcoming_meeting_visibility(visible_meetings)
        self.assert_upcoming_meeting_calendar(visible_meetings)
        self.assert_upcoming_view_filter_matches_ics_filter(querystring)

        # Check the ical links
        simplified_querystring = querystring.replace(' ', '')  # remove spaces
        if simplified_querystring in ['?show=', '?hide=', '?show=&hide=']:
            simplified_querystring = ''  # these empty querystrings will be dropped (not an exhaustive list)

        ics_link = self.driver.find_element(By.LINK_TEXT, 'Download as .ics')
        self.assertIn(simplified_querystring, ics_link.get_attribute('href'))
        webcal_link = self.driver.find_element(By.LINK_TEXT, 'Subscribe with webcal')
        self.assertIn(simplified_querystring, webcal_link.get_attribute('href'))

    def assert_upcoming_view_filter_matches_ics_filter(self, filter_string):
        """The upcoming view and ics view should show matching events for a given filter

        The upcoming ics view shows more detail than the upcoming view, so this
        test expands the upcoming meeting list into the corresponding set of expected
        sessions.
 
        This must be called after using self.driver.get to load the upcoming page
        to be checked.
        """
        ics_url = self.absreverse('ietf.meeting.views.upcoming_ical')

        # parse out the meetings shown on the upcoming view
        upcoming_meetings = self.find_upcoming_meeting_entries()
        visible_meetings = [mtg for mtg in upcoming_meetings if mtg.is_displayed()]
        
        # Have list of meetings, now get sessions that should be shown
        expected_ietfs = []
        expected_interim_sessions = []
        expected_schedules = []
        for meeting_elt in visible_meetings:
            # meeting_elt is an anchor element
            label_text = meeting_elt.get_attribute('innerHTML')
            match = re.search(r'(?P<ietf>IETF\s+)?(?P<number>\S+)', label_text)
            meeting = Meeting.objects.get(number=match.group('number'))
            if match.group('ietf'):
                expected_ietfs.append(meeting)
            else:
                expected_interim_sessions.extend([s.pk for s in meeting.session_set.all()])
                if meeting.schedule:
                    expected_schedules.extend([meeting.schedule, meeting.schedule.base])
        
        # Now find the sessions we expect to see - should match the upcoming_ical view
        expected_assignments = list(SchedTimeSessAssignment.objects.filter(
            schedule__in=expected_schedules,
            session__in=expected_interim_sessions,
            timeslot__time__gte=datetime.date.today(),
        ))
        # The UID formats should match those in the upcoming.ics template
        expected_uids = [
            'ietf-%s-%s' % (item.session.meeting.number, item.timeslot.pk)
            for item in expected_assignments
        ] + [
            'ietf-%s' % (ietf.number) for ietf in expected_ietfs
        ]
        r = self.client.get(ics_url + filter_string)
        assert_ical_response_is_valid(self, r,
                                      expected_event_uids=expected_uids,
                                      expected_event_count=len(expected_uids))

    def test_upcoming_view_default(self):
        """By default, all upcoming interims and IETF meetings should be displayed"""
        ietf_meetings = set(self.all_ietf_meetings())
        self.do_upcoming_view_filter_test('', ietf_meetings.union(self.displayed_interims()))

    def test_upcoming_view_filter_show_group(self):
        # Show none
        ietf_meetings = set(self.all_ietf_meetings())
        self.do_upcoming_view_filter_test('?show=', ietf_meetings)

        # Show one
        self.do_upcoming_view_filter_test('?show=mars', 
                                          ietf_meetings.union(
                                              self.displayed_interims(groups=['mars'])
                                          ))

        # Show two
        self.do_upcoming_view_filter_test('?show=mars,ames', 
                                          ietf_meetings.union(
                                              self.displayed_interims(groups=['mars', 'ames'])
                                          ))

    def test_upcoming_view_filter_show_area(self):
        mars = Group.objects.get(acronym='mars')
        area = mars.parent
        ietf_meetings = set(self.all_ietf_meetings())
        self.do_upcoming_view_filter_test('?show=%s' % area.acronym,
                                          ietf_meetings.union(
                                              self.displayed_interims(groups=['mars', 'ames'])
                                          ))

    def test_upcoming_view_filter_show_type(self):
        ietf_meetings = set(self.all_ietf_meetings())
        self.do_upcoming_view_filter_test('?show=plenary',
                                          ietf_meetings.union(
                                              self.displayed_interims(groups=['sg'])
                                          ))

    def test_upcoming_view_filter_hide_group(self):
        mars = Group.objects.get(acronym='mars')
        area = mars.parent

        # Without anything shown, should see only ietf meetings
        ietf_meetings = set(self.all_ietf_meetings())
        self.do_upcoming_view_filter_test('?hide=mars', ietf_meetings)

        # With group shown
        self.do_upcoming_view_filter_test('?show=ames,mars&hide=mars',
                                          ietf_meetings.union(
                                              self.displayed_interims(groups=['ames'])
                                          ))
        # With area shown
        self.do_upcoming_view_filter_test('?show=%s&hide=mars' % area.acronym, 
                                          ietf_meetings.union(
                                              self.displayed_interims(groups=['ames'])
                                          ))

        # With type shown
        self.do_upcoming_view_filter_test('?show=plenary&hide=sg',
                                          ietf_meetings)

    def test_upcoming_view_filter_hide_area(self):
        mars = Group.objects.get(acronym='mars')
        area = mars.parent

        # Without anything shown, should see only ietf meetings
        ietf_meetings = set(self.all_ietf_meetings())
        self.do_upcoming_view_filter_test('?hide=%s' % area.acronym, ietf_meetings)

        # With area shown
        self.do_upcoming_view_filter_test('?show=%s&hide=%s' % (area.acronym, area.acronym),
                                          ietf_meetings)

        # With group shown
        self.do_upcoming_view_filter_test('?show=mars&hide=%s' % area.acronym, ietf_meetings)

        # With type shown
        self.do_upcoming_view_filter_test('?show=regular&hide=%s' % area.acronym, ietf_meetings)

    def test_upcoming_view_filter_hide_type(self):
        mars = Group.objects.get(acronym='mars')
        area = mars.parent

        # Without anything shown, should see only ietf meetings
        ietf_meetings = set(self.all_ietf_meetings())
        self.do_upcoming_view_filter_test('?hide=regular', ietf_meetings)

        # With group shown
        self.do_upcoming_view_filter_test('?show=mars&hide=regular', ietf_meetings)

        # With type shown
        self.do_upcoming_view_filter_test('?show=plenary,regular&hide=%s' % area.acronym, 
                                          ietf_meetings.union(
                                              self.displayed_interims(groups=['sg'])
                                          ))

    def test_upcoming_view_filter_whitespace(self):
        """Whitespace in filter lists should be ignored"""
        meetings = set(self.all_ietf_meetings())
        meetings.update(self.displayed_interims(groups=['mars']))
        self.do_upcoming_view_filter_test('?show=mars , ames &hide=   ames', meetings)

    def test_upcoming_view_time_zone_selection(self):
        def _assert_interim_tz_correct(sessions, tz):
            zone = pytz.timezone(tz)
            for session in sessions:
                ts = session.official_timeslotassignment().timeslot
                start = ts.utc_start_time().astimezone(zone).strftime('%Y-%m-%d %H:%M')
                end = ts.utc_end_time().astimezone(zone).strftime('%H:%M')
                meeting_link = self.driver.find_element(By.LINK_TEXT, session.meeting.number)
                time_td = meeting_link.find_element(By.XPATH, '../../td[contains(@class, "session-time")]')
                self.assertIn('%s-%s' % (start, end), time_td.text)

        def _assert_ietf_tz_correct(meetings, tz):
            zone = pytz.timezone(tz)
            for meeting in meetings:
                meeting_zone = pytz.timezone(meeting.time_zone)
                start_dt = meeting_zone.localize(datetime.datetime.combine(
                    meeting.date, 
                    datetime.time.min
                ))
                end_dt = meeting_zone.localize(datetime.datetime.combine(
                    start_dt + datetime.timedelta(days=meeting.days - 1),
                    datetime.time.max
                ))
                
                start = start_dt.astimezone(zone).strftime('%Y-%m-%d')
                end = end_dt.astimezone(zone).strftime('%Y-%m-%d')
                meeting_link = self.driver.find_element(By.LINK_TEXT, "IETF " + meeting.number)
                time_td = meeting_link.find_element(By.XPATH, '../../td[contains(@class, "meeting-time")]')
                self.assertIn('%s to %s' % (start, end), time_td.text)

        sessions = [m.session_set.first() for m in self.displayed_interims()]
        self.assertGreater(len(sessions), 0)
        ietf_meetings = self.all_ietf_meetings()
        self.assertGreater(len(ietf_meetings), 0)

        self.driver.get(self.absreverse('ietf.meeting.views.upcoming'))
        tz_select_input = self.driver.find_element(By.ID, 'timezone-select')
        tz_select_bottom_input = self.driver.find_element(By.ID, 'timezone-select-bottom')
        local_tz_link = self.driver.find_element(By.ID, 'local-timezone')
        utc_tz_link = self.driver.find_element(By.ID, 'utc-timezone')
        local_tz_bottom_link = self.driver.find_element(By.ID, 'local-timezone-bottom')
        utc_tz_bottom_link = self.driver.find_element(By.ID, 'utc-timezone-bottom')
        
        # wait for the select box to be updated - look for an arbitrary time zone to be in
        # its options list to detect this
        arbitrary_tz = 'America/Halifax'
        arbitrary_tz_opt = self.wait.until(
            expected_conditions.presence_of_element_located(
                (By.CSS_SELECTOR, '#timezone-select > option[value="%s"]' % arbitrary_tz)
            )
        )

        arbitrary_tz_bottom_opt = tz_select_bottom_input.find_element(By.CSS_SELECTOR,
            '#timezone-select-bottom > option[value="%s"]' % arbitrary_tz)

        utc_tz_opt = tz_select_input.find_element(By.CSS_SELECTOR, 'option[value="UTC"]')
        utc_tz_bottom_opt= tz_select_bottom_input.find_element(By.CSS_SELECTOR, 'option[value="UTC"]')

        # Moment.js guesses local time zone based on the behavior of Selenium's web client. This seems
        # to inherit Django's settings.TIME_ZONE but I don't know whether that's guaranteed to be consistent.
        # To avoid test fragility, ask Moment what it considers local and expect that.
        local_tz = self.driver.execute_script('return moment.tz.guess();')
        local_tz_opt = tz_select_input.find_element(By.CSS_SELECTOR, 'option[value=%s]' % local_tz)
        local_tz_bottom_opt = tz_select_bottom_input.find_element(By.CSS_SELECTOR, 'option[value="%s"]' % local_tz)

        # Should start off in local time zone
        self.assertTrue(local_tz_opt.is_selected())
        self.assertTrue(local_tz_bottom_opt.is_selected())
        _assert_interim_tz_correct(sessions, local_tz)
        _assert_ietf_tz_correct(ietf_meetings, local_tz)

        # click 'utc' button
        self.driver.execute_script("arguments[0].click();", utc_tz_link)  # FIXME-LARS: not working:
        # utc_tz_link.click()
        self.wait.until(expected_conditions.element_to_be_selected(utc_tz_opt))
        self.assertFalse(local_tz_opt.is_selected())
        self.assertFalse(local_tz_bottom_opt.is_selected())
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertFalse(arbitrary_tz_bottom_opt.is_selected())
        self.assertTrue(utc_tz_opt.is_selected())
        self.assertTrue(utc_tz_bottom_opt.is_selected())
        _assert_interim_tz_correct(sessions, 'UTC')
        _assert_ietf_tz_correct(ietf_meetings, 'UTC')

        # click back to 'local'
        self.driver.execute_script("arguments[0].click();", local_tz_link)  # FIXME-LARS: not working:
        # local_tz_link.click()
        self.wait.until(expected_conditions.element_to_be_selected(local_tz_opt))
        self.assertTrue(local_tz_opt.is_selected())
        self.assertTrue(local_tz_bottom_opt.is_selected())
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertFalse(arbitrary_tz_bottom_opt.is_selected())
        self.assertFalse(utc_tz_opt.is_selected())
        self.assertFalse(utc_tz_bottom_opt.is_selected())
        _assert_interim_tz_correct(sessions, local_tz)
        _assert_ietf_tz_correct(ietf_meetings, local_tz)

        # Now select a different item from the select input
        arbitrary_tz_opt.click()
        self.wait.until(expected_conditions.element_to_be_selected(arbitrary_tz_opt))
        self.assertFalse(local_tz_opt.is_selected())
        self.assertFalse(local_tz_bottom_opt.is_selected())
        self.assertTrue(arbitrary_tz_opt.is_selected())
        self.assertTrue(arbitrary_tz_bottom_opt.is_selected())
        self.assertFalse(utc_tz_opt.is_selected())
        self.assertFalse(utc_tz_bottom_opt.is_selected())
        _assert_interim_tz_correct(sessions, arbitrary_tz)
        _assert_ietf_tz_correct(ietf_meetings, arbitrary_tz)

        # Now repeat those tests using the widgets at the bottom of the page
        # click 'utc' button
        self.driver.execute_script("arguments[0].click();", utc_tz_bottom_link)  # FIXME-LARS: not working:
        # utc_tz_bottom_link.click()
        self.wait.until(expected_conditions.element_to_be_selected(utc_tz_opt))
        self.assertFalse(local_tz_opt.is_selected())
        self.assertFalse(local_tz_bottom_opt.is_selected())
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertFalse(arbitrary_tz_bottom_opt.is_selected())
        self.assertTrue(utc_tz_opt.is_selected())
        self.assertTrue(utc_tz_bottom_opt.is_selected())
        _assert_interim_tz_correct(sessions, 'UTC')
        _assert_ietf_tz_correct(ietf_meetings, 'UTC')

        # click back to 'local'
        self.driver.execute_script("arguments[0].click();", local_tz_bottom_link)  # FIXME-LARS: not working:
        # local_tz_bottom_link.click()
        self.wait.until(expected_conditions.element_to_be_selected(local_tz_opt))
        self.assertTrue(local_tz_opt.is_selected())
        self.assertTrue(local_tz_bottom_opt.is_selected())
        self.assertFalse(arbitrary_tz_opt.is_selected())
        self.assertFalse(arbitrary_tz_bottom_opt.is_selected())
        self.assertFalse(utc_tz_opt.is_selected())
        self.assertFalse(utc_tz_bottom_opt.is_selected())
        _assert_interim_tz_correct(sessions, local_tz)
        _assert_ietf_tz_correct(ietf_meetings, local_tz)

        # Now select a different item from the select input
        arbitrary_tz_bottom_opt.click()
        self.wait.until(expected_conditions.element_to_be_selected(arbitrary_tz_opt))
        self.assertFalse(local_tz_opt.is_selected())
        self.assertFalse(local_tz_bottom_opt.is_selected())
        self.assertTrue(arbitrary_tz_opt.is_selected())
        self.assertTrue(arbitrary_tz_bottom_opt.is_selected())
        self.assertFalse(utc_tz_opt.is_selected())
        self.assertFalse(utc_tz_bottom_opt.is_selected())
        _assert_interim_tz_correct(sessions, arbitrary_tz)
        _assert_ietf_tz_correct(ietf_meetings, arbitrary_tz)

    def test_upcoming_materials_modal(self):
        """Test opening and closing a materials modal

        This does not test dynamic reloading of the meeting materials - it relies on the main
        agenda page testing that. If the materials modal handling diverges between here and
        there, this should be updated to include that test.
        """
        url = self.absreverse('ietf.meeting.views.upcoming')
        self.driver.get(url)

        interim = self.displayed_interims(['mars'])[0]
        session = interim.session_set.first()
        assignment = session.official_timeslotassignment()
        slug = assignment.slug()

        # modal should start hidden
        modal_div = self.driver.find_element(By.CSS_SELECTOR, 'div#modal-%s' % slug)
        self.assertFalse(modal_div.is_displayed())

        # Click the 'materials' button
        open_modal_button = self.wait.until(
            expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, '[data-bs-target="#modal-%s"]' % slug)
            ),
            'Modal open button not found or not clickable',
        )
        open_modal_button.click()
        self.wait.until(
            expected_conditions.visibility_of(modal_div),
            'Modal did not become visible after clicking open button',
        )

        # Now close the modal
        close_modal_button = self.wait.until(
            presence_of_element_child_by_css_selector(
                modal_div,
                '.modal-footer button[data-bs-dismiss="modal"]',
            ),
            'Modal close button not found or not clickable',
        )
        close_modal_button.click()
        self.wait.until(
            expected_conditions.invisibility_of_element(modal_div),
            'Modal was not hidden after clicking close button',
        )


@ifSeleniumEnabled
class ProceedingsMaterialTests(IetfSeleniumTestCase):
    def setUp(self):
        super().setUp()
        self.wait = WebDriverWait(self.driver, 2)
        self.meeting = MeetingFactory(type_id='ietf', number='123', date=datetime.date.today())

    def test_add_proceedings_material(self):
        url = self.absreverse(
            'ietf.meeting.views_proceedings.upload_material',
            kwargs=dict(num=self.meeting.number, material_type='supporters'),
        )
        self.login('secretary')
        self.driver.get(url)

        # get the UI elements
        use_url_checkbox = self.wait.until(
            expected_conditions.element_to_be_clickable((By.ID, 'id_use_url'))
        )
        choose_file_button = self.wait.until(
            expected_conditions.presence_of_element_located((By.ID, 'id_file'))
        )
        external_url_field = self.wait.until(
            expected_conditions.presence_of_element_located((By.ID, 'id_external_url'))
        )

        # should start with use_url unchecked for a new material
        self.assertTrue(choose_file_button.is_displayed(),
                        'File chooser should be shown by default')
        self.assertFalse(external_url_field.is_displayed(),
                         'URL field should be hidden by default')

        # enable URL
        use_url_checkbox.click()
        self.wait.until(expected_conditions.invisibility_of_element(choose_file_button),
                         'File chooser should be hidden when URL option is checked')
        self.wait.until(expected_conditions.visibility_of(external_url_field),
                        'URL field should appear when URL option is checked')

        # disable URL
        use_url_checkbox.click()
        self.wait.until(expected_conditions.visibility_of(choose_file_button),
                        'File chooser should appear when URL option is unchecked')
        self.wait.until(expected_conditions.invisibility_of_element(external_url_field),
                        'URL field should be hidden when URL option is unchecked')

    def test_replace_proceedings_material_shows_correct_default(self):
        doc_mat = ProceedingsMaterialFactory(meeting=self.meeting)
        url_mat = ProceedingsMaterialFactory(meeting=self.meeting, document__external_url='https://example.com')

        # check the document material
        url = self.absreverse(
            'ietf.meeting.views_proceedings.upload_material',
            kwargs=dict(num=self.meeting.number, material_type=doc_mat.type.slug),
        )
        self.login('secretary')
        self.driver.get(url)
        use_url_checkbox = self.wait.until(
            expected_conditions.element_to_be_clickable((By.ID, 'id_use_url'))
        )
        choose_file_button = self.wait.until(
            expected_conditions.presence_of_element_located((By.ID, 'id_file'))
        )
        external_url_field = self.wait.until(
            expected_conditions.presence_of_element_located((By.ID, 'id_external_url'))
        )

        # should start with use_url unchecked for a document material
        self.assertFalse(use_url_checkbox.is_selected(), 'URL option should be unchecked for a document material')
        self.assertTrue(choose_file_button.is_displayed(),
                        'File chooser should be shown by default')
        self.assertFalse(external_url_field.is_displayed(),
                         'URL field should be hidden by default')

        # check the URL material
        url = self.absreverse(
            'ietf.meeting.views_proceedings.upload_material',
            kwargs=dict(num=self.meeting.number, material_type=url_mat.type.slug),
        )
        self.driver.get(url)

        use_url_checkbox = self.wait.until(
            expected_conditions.element_to_be_clickable((By.ID, 'id_use_url'))
        )
        choose_file_button = self.wait.until(
            expected_conditions.presence_of_element_located((By.ID, 'id_file'))
        )
        external_url_field = self.wait.until(
            expected_conditions.presence_of_element_located((By.ID, 'id_external_url'))
        )

        # should start with use_url unchecked for a document material
        self.assertTrue(use_url_checkbox.is_selected(), 'URL option should be checked for URL material')
        self.assertFalse(choose_file_button.is_displayed(),
                         'File chooser should be hidden by default')
        self.assertTrue(external_url_field.is_displayed(),
                        'URL field should be shown by default')


@ifSeleniumEnabled
class EditTimeslotsTests(IetfSeleniumTestCase):
    """Test the timeslot editor"""
    def setUp(self):
        super().setUp()
        self.meeting: Meeting = MeetingFactory(
            type_id='ietf',
            number=120,
            date=datetime.datetime.today() + datetime.timedelta(days=10),
            populate_schedule=False,
        )
        self.edit_timeslot_url = self.absreverse(
            'ietf.meeting.views.edit_timeslots',
            kwargs=dict(num=self.meeting.number),
        )
        self.wait = WebDriverWait(self.driver, 2)

    def do_delete_test(self, selector, keep, delete, cancel=False):
        self.login('secretary')
        self.driver.get(self.edit_timeslot_url)
        delete_button = self.wait.until(
            expected_conditions.element_to_be_clickable(
                (By.CSS_SELECTOR, selector)
            ))
        delete_button.click()

        if cancel:
            cancel_button = self.wait.until(
                expected_conditions.element_to_be_clickable(
                    (By.CSS_SELECTOR, '#delete-modal button[data-bs-dismiss="modal"]')
                ))
            cancel_button.click()
        else:
            confirm_button = self.wait.until(
                expected_conditions.element_to_be_clickable(
                    (By.CSS_SELECTOR, '#confirm-delete-button')
                ))
            confirm_button.click()

        self.wait.until(
            expected_conditions.invisibility_of_element_located(
                (By.CSS_SELECTOR, '#delete-modal')
            ))

        if cancel:
            keep.extend(delete)
            delete = []

        self.assertEqual(
            TimeSlot.objects.filter(pk__in=[ts.pk for ts in delete]).count(),
            0,
            'Not all expected timeslots deleted',
        )
        self.assertEqual(
            TimeSlot.objects.filter(pk__in=[ts.pk for ts in keep]).count(),
            len(keep),
            'Not all expected timeslots kept'
        )

    def do_delete_timeslot_test(self, cancel=False):
        delete = [TimeSlotFactory(meeting=self.meeting)]
        keep = [TimeSlotFactory(meeting=self.meeting)]

        self.do_delete_test(
            '#timeslot-table #timeslot{} .delete-button'.format(delete[0].pk),
            keep,
            delete
        )

    def test_delete_timeslot(self):
        """Delete button for a timeslot should delete that timeslot"""
        self.do_delete_timeslot_test(cancel=False)

    def test_delete_timeslot_cancel(self):
        """Timeslot should not be deleted on cancel"""
        self.do_delete_timeslot_test(cancel=True)

    def do_delete_time_interval_test(self, cancel=False):
        delete_day = self.meeting.date.date()
        delete_time = datetime.time(hour=10)
        other_day = self.meeting.get_meeting_date(1).date()
        other_time = datetime.time(hour=12)
        duration = datetime.timedelta(minutes=60)

        delete: [TimeSlot] = TimeSlotFactory.create_batch(
            2,
            meeting=self.meeting,
            time=datetime.datetime.combine(delete_day, delete_time),
            duration=duration)

        keep: [TimeSlot] = [
            TimeSlotFactory(
                meeting=self.meeting,
                time=datetime.datetime.combine(day, time),
                duration=duration
            )
            for (day, time) in (
                # combinations of day/time that should not be deleted
                (delete_day, other_time),
                (other_day, delete_time),
                (other_day, other_time),
            )
        ]

        selector = (
            '#timeslot-table '
            '.delete-button[data-delete-scope="column"]'
            '[data-col-id="{}T{}-{}"]'.format(
                delete_day.isoformat(),
                delete_time.strftime('%H:%M'),
                (datetime.datetime.combine(delete_day, delete_time) + duration).strftime(
                    '%H:%M'
                ))
        )
        self.do_delete_test(selector, keep, delete, cancel)

    def test_delete_time_interval(self):
        """Delete button for a time interval should delete all timeslots in that interval"""
        self.do_delete_time_interval_test(cancel=False)

    def test_delete_time_interval_cancel(self):
        """Should not delete a time interval on cancel"""
        self.do_delete_time_interval_test(cancel=True)

    def do_delete_day_test(self, cancel=False):
        delete_day = self.meeting.date.date()
        times = [datetime.time(hour=10), datetime.time(hour=12)]
        other_days = [self.meeting.get_meeting_date(d).date() for d in range(1, 3)]

        delete: [TimeSlot] = [
            TimeSlotFactory(
                meeting=self.meeting,
                time=datetime.datetime.combine(delete_day, time),
            ) for time in times
        ]

        keep: [TimeSlot] = [
            TimeSlotFactory(
                meeting=self.meeting,
                time=datetime.datetime.combine(day, time),
            ) for day in other_days for time in times
        ]

        selector = (
            '#timeslot-table '
            '.delete-button[data-delete-scope="day"][data-date-id="{}"]'.format(
                delete_day.isoformat()
            )
        )
        self.do_delete_test(selector, keep, delete, cancel)

    def test_delete_day(self):
        """Delete button for a day should delete all timeslots on that day"""
        self.do_delete_day_test(cancel=False)

    def test_delete_day_cancel(self):
        """Should not delete a day on cancel"""
        self.do_delete_day_test(cancel=True)


# The following are useful debugging tools

# If you add this to a LiveServerTestCase and run just this test, you can browse
# to the test server with the data loaded by setUp() to debug why, for instance,
# a particular view isn't giving you what you expect
#    def testJustSitThere(self):
#        time.sleep(10000)

# The LiveServerTestCase server runs in a mode like production - it hides crashes with the
# user-friendly message about mail being sent to the maintainers, and eats that mail.
# Loading the page that crashed with just a TestCase will at least let you see the
# traceback.
#
#from ietf.utils.test_utils import TestCase
#class LookAtCrashTest(TestCase):
#    def setUp(self):
#        make_meeting_test_data()
#
#    def testOpenSchedule(self):
#        url = urlreverse('ietf.meeting.views.edit_meeting_schedule', kwargs=dict(num='72',name='test-schedule'))
#        r = self.client.get(url)