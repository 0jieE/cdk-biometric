"""Special holiday with attendance required (an "event day").

The admin ticks which session(s) are required (AM, PM or both). Completing
them credits the WHOLE day as present - nothing late, undertime or absent.
Not completing them makes it an ordinary workday (absent / half day as
usual), and the usual holiday bridging exemption doesn't apply.
"""

from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.api.selectors import build_daily_attendance
from apps.attendance.models import Absence, AttendanceLog, Lates, Undertime
from apps.attendance.services import process_day
from apps.organization.models import Department, Employee, GlobalSchedule, Holiday
from apps.webportal.forms import HolidayForm

TUE, WED, THU = date(2026, 7, 7), date(2026, 7, 8), date(2026, 7, 9)
User = get_user_model()


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()   # 08:00-12:00, 13:00-17:00, grace 5, Mon-Fri
        cls.dept = Department.objects.create(name='IT', code='IT')
        cls.ft = Employee.objects.create(employee_no='EV-1', first_name='Full', last_name='Time',
                                         department=cls.dept, biometric_id='7701', is_fulltime=True)
        cls.pt = Employee.objects.create(employee_no='EV-2', first_name='Part', last_name='Time',
                                         department=cls.dept, biometric_id='7702', is_fulltime=False)
        # Tracking starts at max(first punch ever, created_at): anchor both in June.
        anchor = Employee.objects.create(employee_no='EV-X', first_name='An', last_name='Chor',
                                         department=cls.dept, biometric_id='7799', is_fulltime=True)
        AttendanceLog.objects.create(employee=anchor, log_type='AM_IN',
                                     log_datetime=aware(date(2026, 6, 1), time(8, 0)))
        Employee.objects.filter(pk__in=[cls.ft.pk, cls.pt.pk]).update(
            created_at=aware(date(2026, 6, 1), time(0, 0)))
        cls.ft.refresh_from_db()
        cls.pt.refresh_from_db()

    def _event(self, am=False, pm=False, **kw):
        return Holiday.objects.create(date=WED, name='Foundation Day', type=Holiday.Types.SPECIAL,
                                      attendance_required=True, required_am=am, required_pm=pm, **kw)

    def _punch(self, d, *pairs, emp=None):
        for log_type, t in pairs:
            AttendanceLog.objects.create(employee=emp or self.ft, log_type=log_type,
                                         log_datetime=aware(d, t))

    def _full_day(self, d):
        self._punch(d, ('AM_IN', time(8, 0)), ('AM_OUT', time(12, 0)),
                    ('PM_IN', time(13, 0)), ('PM_OUT', time(17, 0)))

    def _day(self):
        return build_daily_attendance(self.ft, WED, WED)[0]


class EventDayProcessingTests(_Base):
    def test_attending_the_required_am_credits_the_whole_day(self):
        self._event(am=True)
        self._punch(WED, ('AM_IN', time(8, 30)), ('AM_OUT', time(11, 0)))  # late + early out
        self.assertEqual(process_day(self.ft, WED)['status'], 'PRESENT')
        self.assertFalse(Absence.objects.filter(employee=self.ft).exists())   # no PM absence
        self.assertFalse(Lates.objects.exists())
        self.assertFalse(Undertime.objects.exists())
        rec = self._day()
        self.assertEqual((rec['day_status'], rec['lost_minutes']), ('PRESENT', 0))
        self.assertEqual(rec['event'], 'Foundation Day (AM required)')

    def test_pm_only_requirement(self):
        self._event(pm=True)
        self._punch(WED, ('PM_IN', time(13, 0)), ('PM_OUT', time(17, 0)))
        self.assertEqual(process_day(self.ft, WED)['status'], 'PRESENT')
        self.assertEqual(self._day()['day_status'], 'PRESENT')

    def test_both_required_and_both_attended(self):
        self._event(am=True, pm=True)
        self._full_day(WED)
        self.assertEqual(process_day(self.ft, WED)['status'], 'PRESENT')

    def test_missing_the_requirement_is_a_whole_day_absence(self):
        self._event(am=True)
        # Present on both neighbours: on an ordinary holiday that makes the day
        # exempt (bridging) - an event day ignores that.
        self._full_day(TUE)
        self._full_day(THU)
        self.assertEqual(process_day(self.ft, WED)['status'], 'ABSENT')
        self.assertEqual(sum(Absence.objects.filter(employee=self.ft, date=WED)
                             .values_list('minutes_absent', flat=True)), 480)
        rec = self._day()
        self.assertEqual((rec['day_status'], rec['lost_minutes']), ('ABSENT', 480))
        self.assertNotIn('event', rec)

    def test_attending_only_the_unrequired_half_is_a_half_day(self):
        self._event(am=True)
        self._punch(WED, ('PM_IN', time(13, 0)), ('PM_OUT', time(17, 0)))
        self.assertEqual(process_day(self.ft, WED)['status'], 'HALF_DAY')
        self.assertEqual(Absence.objects.get(employee=self.ft, date=WED).session, 'AM')

    def test_both_required_but_only_am_attended_is_a_half_day(self):
        self._event(am=True, pm=True)
        self._punch(WED, ('AM_IN', time(8, 0)), ('AM_OUT', time(12, 0)))
        self.assertEqual(process_day(self.ft, WED)['status'], 'HALF_DAY')
        self.assertEqual(self._day()['day_status'], 'HALF_DAY')

    def test_undecided_while_the_required_session_is_still_running(self):
        self._event(am=True)
        self._punch(WED, ('AM_IN', time(8, 0)))
        result = process_day(self.ft, WED, now=aware(WED, time(10, 0)))
        self.assertEqual(result['status'], 'PENDING')
        self.assertFalse(Absence.objects.exists())

    def test_part_timers_are_unaffected(self):
        self._event(am=True)
        self.assertEqual(process_day(self.pt, WED)['status'], 'REST')
        self.assertFalse(Absence.objects.filter(employee=self.pt).exists())

    def test_a_regular_holiday_keeps_the_bridging_rule(self):
        Holiday.objects.create(date=WED, name='Regular', type=Holiday.Types.REGULAR)
        self._full_day(TUE)
        self._full_day(THU)
        self.assertEqual(process_day(self.ft, WED)['status'], 'REST')
        self.assertEqual(build_daily_attendance(self.ft, WED, WED), [])


class HolidayModelAndFormTests(_Base):
    def test_only_a_special_holiday_keeps_the_requirement(self):
        h = Holiday.objects.create(date=WED, name='X', type=Holiday.Types.REGULAR,
                                   attendance_required=True, required_am=True)
        h.refresh_from_db()
        self.assertEqual((h.attendance_required, h.required_am, h.required_sessions),
                         (False, False, ()))

    def test_required_sessions_and_label(self):
        h = self._event(am=True, pm=True)
        self.assertEqual((h.required_sessions, h.required_label), (('AM', 'PM'), 'AM & PM'))

    def _form(self, **extra):
        return HolidayForm({'date': WED, 'name': 'Event', 'type': 'SPECIAL', 'color': '#F59E0B',
                            **extra})

    def test_form_needs_a_session_when_attendance_is_required(self):
        form = self._form(attendance_required='on')
        self.assertFalse(form.is_valid())
        self.assertIn('required_am', form.errors)

    def test_form_drops_the_sessions_when_attendance_is_not_required(self):
        form = self._form(required_am='on')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().required_sessions, ())


class HolidaySaveReprocessesTests(_Base):
    """Saving / deleting a holiday re-judges that (past) date at once."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = User.objects.create(username='admin-ev', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password('evpass12345')
        cls.admin.save()

    def setUp(self):
        self.client.login(username='admin-ev', password='evpass12345')
        self._full_day(TUE)
        self._full_day(THU)
        process_day(self.ft, WED)                    # an ordinary workday: absent
        self.assertEqual(Absence.objects.filter(employee=self.ft, date=WED).count(), 2)

    def _post(self, url, **data):
        return self.client.post(url, {'date': WED.isoformat(), 'name': 'Foundation Day',
                                      'color': '#F59E0B', **data})

    def test_adding_a_holiday_clears_the_absence(self):
        self._post(reverse('webportal:holiday_create'), type='SPECIAL')
        self.assertFalse(Absence.objects.filter(employee=self.ft, date=WED).exists())

    def test_requiring_attendance_brings_it_back_and_deleting_keeps_it(self):
        self._post(reverse('webportal:holiday_create'), type='SPECIAL')
        h = Holiday.objects.get(date=WED)
        self._post(reverse('webportal:holiday_edit', args=[h.pk]), type='SPECIAL',
                   attendance_required='on', required_am='on')
        self.assertEqual(Absence.objects.filter(employee=self.ft, date=WED).count(), 2)
        self.client.post(reverse('webportal:holiday_delete', args=[h.pk]))
        self.assertEqual(Absence.objects.filter(employee=self.ft, date=WED).count(), 2)
