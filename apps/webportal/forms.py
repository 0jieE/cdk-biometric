from datetime import timedelta

from django import forms
from django.utils import timezone

from apps.attendance.models import AttendanceLog, OTAuthorization
from apps.devices.models import BiometricDevice
from apps.organization.models import (
    Department,
    Employee,
    EmployeeSchedule,
    GlobalSchedule,
    Holiday,
)
from apps.reports.generators import MAX_RANGE_DAYS
from apps.reports.models import ReportJob

WEEKDAYS = [(0, 'Mon'), (1, 'Tue'), (2, 'Wed'), (3, 'Thu'),
            (4, 'Fri'), (5, 'Sat'), (6, 'Sun')]


class BootstrapMixin:
    """Add Bootstrap 5 classes to every field widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput,)):
                widget.attrs.setdefault('class', 'form-check-input')
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault('class', 'form-select')
            else:
                widget.attrs.setdefault('class', 'form-control')


class EmployeeForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Employee
        fields = ('employee_no', 'first_name', 'last_name', 'department',
                  'is_fulltime', 'biometric_id', 'date_hired',
                  'photo', 'is_active')
        widgets = {
            'date_hired': forms.DateInput(attrs={'type': 'date'}),
        }


class DepartmentForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Department
        fields = ('name', 'code', 'banner', 'logo', 'is_active')


class HolidayForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Holiday
        fields = ('date', 'name', 'type', 'color', 'is_recurring',
                  'attendance_required', 'required_am', 'required_pm')
        labels = {'attendance_required': 'Attendance required',
                  'required_am': 'AM attendance', 'required_pm': 'PM attendance'}
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'color': forms.TextInput(attrs={'type': 'color', 'class': 'form-control form-control-color'}),
        }

    def clean(self):
        data = super().clean()
        if data.get('type') != Holiday.Types.SPECIAL or not data.get('attendance_required'):
            data['attendance_required'] = data['required_am'] = data['required_pm'] = False
        elif not (data.get('required_am') or data.get('required_pm')):
            self.add_error('required_am', 'Tick AM, PM or both: which punches are required?')
        return data


class DeviceForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = BiometricDevice
        fields = ('name', 'ip_address', 'port', 'location', 'serial_no',
                  'photo', 'is_active')


class _EmployeeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, emp):
        # Inactive staff stay selectable: a record for someone who left is legit.
        return f'{emp.employee_no} — {emp.full_name}' + ('' if emp.is_active else ' (inactive)')


class ReportForm(BootstrapMixin, forms.Form):
    """The Reports page: the Attendance page's table, as a PDF/Excel file."""

    employee = _EmployeeChoiceField(
        queryset=Employee.objects.order_by('employee_no'), empty_label='Select an employee')
    start = forms.DateField(label='Start', widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='End', widget=forms.DateInput(attrs={'type': 'date'}))
    fmt = forms.ChoiceField(choices=ReportJob.Fmt.choices, label='Format')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Same default window as the Attendance page.
        today = timezone.localdate()
        self.fields['start'].initial = today - timedelta(days=7)
        self.fields['end'].initial = today

    def clean(self):
        cd = super().clean()
        start, end = cd.get('start'), cd.get('end')
        if start and end:
            if start > end:
                self.add_error('end', 'End date must be on or after the start date.')
            elif (end - start).days + 1 > MAX_RANGE_DAYS:
                self.add_error('end', f'Range is too long (max {MAX_RANGE_DAYS} days).')
        return cd

    def to_params(self) -> dict:
        cd = self.cleaned_data
        return {'employee': cd['employee'].id,
                'start': cd['start'].isoformat(),
                'end': cd['end'].isoformat()}


class _WorkdaysMixin:
    """Renders workdays as Mon–Sun checkboxes and stores a JSON list of ints."""

    def _build_workdays_field(self, required):
        return forms.MultipleChoiceField(
            choices=WEEKDAYS, required=required,
            widget=forms.CheckboxSelectMultiple(attrs={'class': ''}))

    def clean_workdays(self):
        values = self.cleaned_data.get('workdays')
        if values in (None, ''):
            return None
        return sorted(int(v) for v in values)


def check_schedule_order(form, times):
    """AM in < AM out <= midpoint <= PM in < PM out - the order every rule
    relies on (sorting scans, lunch, the Punch Terminal's windows). ``times``
    maps each field to its effective value; errors go on ``form``'s fields."""
    def fmt(t):
        return t.strftime('%I:%M %p').lstrip('0')

    am_in, am_out, mid, pm_in, pm_out = (
        times.get(f) for f in ('am_in', 'am_out', 'midpoint', 'pm_in', 'pm_out'))
    if None in (am_in, am_out, mid, pm_in, pm_out):
        return
    if not am_in < am_out:
        form.add_error('am_out', f'Must be after the AM time in ({fmt(am_in)}).')
    elif not am_out <= pm_in:
        form.add_error('pm_in', f'Must be at or after the AM time out ({fmt(am_out)}).')
    elif not pm_in < pm_out:
        form.add_error('pm_out', f'Must be after the PM time in ({fmt(pm_in)}).')
    elif not am_out <= mid <= pm_in:
        form.add_error('midpoint', f'Must fall between the AM time out ({fmt(am_out)}) '
                                   f'and the PM time in ({fmt(pm_in)}).')


class GlobalScheduleForm(_WorkdaysMixin, BootstrapMixin, forms.ModelForm):
    workdays = None  # replaced in __init__

    class Meta:
        model = GlobalSchedule
        fields = ('am_in', 'am_out', 'pm_in', 'pm_out', 'midpoint',
                  'grace_period_minutes', 'workdays')
        widgets = {f: forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')
                   for f in ('am_in', 'am_out', 'pm_in', 'pm_out', 'midpoint')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ('am_in', 'am_out', 'pm_in', 'pm_out', 'midpoint'):
            self.fields[f].input_formats = ['%H:%M', '%H:%M:%S']
        self.fields['midpoint'].label = 'AM/PM midpoint'
        self.fields['midpoint'].help_text = 'Punches up to this time are AM, after it PM.'
        self.fields['workdays'] = self._build_workdays_field(required=True)
        if self.instance and self.instance.pk:
            self.fields['workdays'].initial = self.instance.workdays

    def clean(self):
        data = super().clean()
        check_schedule_order(self, data)
        return data


class EmployeeScheduleForm(_WorkdaysMixin, BootstrapMixin, forms.ModelForm):
    class Meta:
        model = EmployeeSchedule
        fields = ('am_in', 'am_out', 'pm_in', 'pm_out', 'midpoint',
                  'grace_period_minutes', 'workdays')
        widgets = {f: forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')
                   for f in ('am_in', 'am_out', 'pm_in', 'pm_out', 'midpoint')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Every field optional — blank means "inherit global".
        for name, field in self.fields.items():
            field.required = False
            if name in ('am_in', 'am_out', 'pm_in', 'pm_out', 'midpoint'):
                field.input_formats = ['%H:%M', '%H:%M:%S']
        self.fields['workdays'] = self._build_workdays_field(required=False)
        if self.instance and self.instance.pk and self.instance.workdays:
            self.fields['workdays'].initial = self.instance.workdays

    def clean(self):
        data = super().clean()
        # Blank fields inherit the global schedule, so check what will apply.
        g = GlobalSchedule.load()
        check_schedule_order(self, {
            f: data.get(f) or getattr(g, f)
            for f in ('am_in', 'am_out', 'midpoint', 'pm_in', 'pm_out')})
        return data


class ParttimeDefaultHoursForm(BootstrapMixin, forms.ModelForm):
    """The weekly hours every part-timer owes unless set per person, and the
    fewest different days those hours must be spread over."""

    class Meta:
        model = GlobalSchedule
        fields = ('parttime_weekly_hours', 'parttime_min_days')
        labels = {'parttime_weekly_hours': 'Default weekly hours',
                  'parttime_min_days': 'Minimum days'}
        widgets = {'parttime_weekly_hours': forms.NumberInput(
                       attrs={'step': '0.5', 'min': '0.5', 'max': '168'}),
                   'parttime_min_days': forms.NumberInput(
                       attrs={'step': '1', 'min': '1', 'max': '7'})}

    def clean_parttime_weekly_hours(self):
        hours = self.cleaned_data['parttime_weekly_hours']
        if not 0 < hours <= 168:
            raise forms.ValidationError('Enter between 0.5 and 168 hours.')
        return hours

    def clean_parttime_min_days(self):
        days = self.cleaned_data['parttime_min_days']
        if not 1 <= days <= 7:
            raise forms.ValidationError('Enter between 1 and 7 days.')
        return days


class EmployeeWeeklyHoursForm(BootstrapMixin, forms.ModelForm):
    """One part-timer's own weekly hours; blank = the default."""

    class Meta:
        model = EmployeeSchedule
        fields = ('weekly_hours',)
        labels = {'weekly_hours': 'Weekly hours'}
        widgets = {'weekly_hours': forms.NumberInput(
            attrs={'step': '0.5', 'min': '0.5', 'max': '168'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['weekly_hours'].required = False

    def clean_weekly_hours(self):
        hours = self.cleaned_data.get('weekly_hours')
        if hours is not None and not 0 < hours <= 168:
            raise forms.ValidationError('Enter between 0.5 and 168 hours, or leave blank.')
        return hours


class OTAuthorizationForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = OTAuthorization
        fields = ('employee', 'date', 'ot_start', 'ot_end_expected', 'note')
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'ot_start': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
            'ot_end_expected': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Overtime is full-time only.
        self.fields['employee'].queryset = Employee.objects.filter(
            is_active=True, is_fulltime=True).order_by('employee_no')
        for f in ('ot_start', 'ot_end_expected'):
            self.fields[f].input_formats = ['%H:%M', '%H:%M:%S']


class ManualAttendanceForm(forms.Form):
    """Device-style manual punch. Date/time default to *now*; valid punch types
    depend on the employee's type; OT types require an authorization for today."""

    FULLTIME_TYPES = ['AM_IN', 'AM_OUT', 'PM_IN', 'PM_OUT', 'OT_IN', 'OT_OUT']
    PARTTIME_TYPES = ['IN', 'OUT']

    employee = forms.ModelChoiceField(
        queryset=Employee.objects.filter(is_active=True).order_by('employee_no'))
    log_type = forms.ChoiceField(choices=AttendanceLog.LogType.choices)

    def clean(self):
        cd = super().clean()
        emp = cd.get('employee')
        log_type = cd.get('log_type')
        if not (emp and log_type):
            return cd

        valid = self.FULLTIME_TYPES if emp.is_fulltime else self.PARTTIME_TYPES
        if log_type not in valid:
            raise forms.ValidationError(
                f"{log_type} is not valid for a "
                f"{'full-time' if emp.is_fulltime else 'part-time'} employee.")

        self._reject_outside_session(emp, log_type)

        if log_type in ('OT_IN', 'OT_OUT'):
            from django.utils import timezone
            if not OTAuthorization.objects.filter(
                    employee=emp, date=timezone.localdate()).exists():
                raise forms.ValidationError(
                    'OT punches require an OT authorization for this employee '
                    'today (authorize it on the Overtime page first).')

        self._reject_duplicate(emp, log_type)
        return cd

    def _reject_outside_session(self, emp, log_type):
        """A session's In is closed once its time out has passed (12:00 PM for
        AM In), and its Out isn't open until its time in (8:00 AM for AM Out).
        AM Out also closes when the afternoon starts (1:00 PM), just as a
        device scan from then on can't be the AM Out, and PM In only opens at
        the midpoint (12:30 PM), where device scans become afternoon. By the employee's own
        schedule and the server's trusted clock; the Punch Terminal shows the
        same rule (see manual_attendance.html)."""
        if log_type not in ('AM_IN', 'AM_OUT', 'PM_IN', 'PM_OUT'):
            return
        from django.utils import timezone

        from apps.organization.schedule import get_effective_schedule
        from apps.organization.trusted_time import trusted_now

        sched = get_effective_schedule(emp)
        now = timezone.localtime(trusted_now()).time()
        session = 'morning' if log_type.startswith('AM') else 'afternoon'
        sched_in = sched.am_in if session == 'morning' else sched.pm_in
        sched_out = sched.am_out if session == 'morning' else sched.pm_out
        if session == 'morning':
            # A scan after the midpoint is never morning, so AM In can't
            # outlast it - even if the AM time out were set later by mistake.
            sched_out = min(sched_out, sched.midpoint)

        def fmt(t):
            return t.strftime('%I:%M %p').lstrip('0')

        if log_type.endswith('_IN') and now >= sched_out:
            raise forms.ValidationError(
                f'{log_type} is closed: the {session} session ended at {fmt(sched_out)}.')
        if log_type.endswith('_OUT') and now < sched_in:
            raise forms.ValidationError(
                f'{log_type} is not open yet: the {session} session starts at {fmt(sched_in)}.')
        if log_type == 'AM_OUT' and now >= sched.pm_in:
            raise forms.ValidationError(
                f'AM_OUT is closed: the afternoon session started at {fmt(sched.pm_in)}.')
        if log_type == 'PM_IN' and now < sched.midpoint:
            raise forms.ValidationError(
                f'PM_IN is not open yet: it opens at the midpoint, {fmt(sched.midpoint)}.')

    # OUT type -> the IN it closes.
    _IN_FOR_OUT = {'AM_OUT': 'AM_IN', 'PM_OUT': 'PM_IN', 'OT_OUT': 'OT_IN', 'OUT': 'IN'}

    def _reject_duplicate(self, emp, log_type):
        """Same rule as device punches (DUPLICATE_PUNCH_WINDOW): within 5 minutes,
        the same punch again - or an OUT right after its IN - is a double click,
        not a new punch."""
        from django.utils import timezone

        from apps.attendance.services import DUPLICATE_PUNCH_WINDOW
        from apps.organization.trusted_time import trusted_now

        now = trusted_now()
        types = [log_type] + ([self._IN_FOR_OUT[log_type]] if log_type in self._IN_FOR_OUT else [])
        recent = (AttendanceLog.objects
                  .filter(employee=emp, log_type__in=types,
                          log_datetime__gt=now - DUPLICATE_PUNCH_WINDOW,
                          log_datetime__lte=now)
                  .order_by('-log_datetime').first())
        if recent is None:
            return
        ago = max(0, int((now - recent.log_datetime).total_seconds() // 60))
        when = timezone.localtime(recent.log_datetime).strftime('%I:%M %p').lstrip('0')
        what = 'just now' if ago == 0 else f'{ago} min ago'
        if recent.log_type == log_type:
            msg = (f'Duplicate: {emp.full_name} already has {log_type} at {when} ({what}). '
                   f'Punches within 5 minutes count as the same punch.')
        else:
            msg = (f'Too soon: {emp.full_name} recorded {recent.log_type} at {when} ({what}). '
                   f'A punch within 5 minutes of it is a duplicate, not the {log_type}.')
        raise forms.ValidationError(msg)
