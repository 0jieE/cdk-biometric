from django import forms

from apps.attendance.models import AttendanceLog, OTAuthorization
from apps.devices.models import BiometricDevice
from apps.organization.models import (
    Department,
    Employee,
    EmployeeSchedule,
    GlobalSchedule,
    Holiday,
)
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
                  'position', 'is_fulltime', 'biometric_id', 'date_hired',
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
        fields = ('date', 'name', 'type', 'color', 'is_recurring')
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'color': forms.TextInput(attrs={'type': 'color', 'class': 'form-control form-control-color'}),
        }


class DeviceForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = BiometricDevice
        fields = ('name', 'ip_address', 'port', 'location', 'serial_no',
                  'photo', 'is_active')


class ReportForm(BootstrapMixin, forms.Form):
    report_type = forms.ChoiceField(choices=ReportJob.ReportType.choices)
    fmt = forms.ChoiceField(choices=ReportJob.Fmt.choices, label='Format')
    department = forms.ModelChoiceField(
        queryset=Department.objects.all(), required=False, empty_label='All departments')
    date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                           help_text='For Daily Attendance')
    month = forms.CharField(required=False, widget=forms.DateInput(attrs={'type': 'month'}),
                            help_text='For Monthly Summary (YYYY-MM)')
    start = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                            help_text='For Tardiness / Absence')
    end = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def to_params(self) -> dict:
        cd = self.cleaned_data
        params = {}
        if cd.get('date'):
            params['date'] = cd['date'].isoformat()
        if cd.get('month'):
            params['month'] = cd['month']
        if cd.get('start'):
            params['start'] = cd['start'].isoformat()
        if cd.get('end'):
            params['end'] = cd['end'].isoformat()
        if cd.get('department'):
            params['department'] = cd['department'].id
        return params


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


class GlobalScheduleForm(_WorkdaysMixin, BootstrapMixin, forms.ModelForm):
    workdays = None  # replaced in __init__

    class Meta:
        model = GlobalSchedule
        fields = ('am_in', 'am_out', 'pm_in', 'pm_out', 'grace_period_minutes', 'workdays')
        widgets = {f: forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')
                   for f in ('am_in', 'am_out', 'pm_in', 'pm_out')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ('am_in', 'am_out', 'pm_in', 'pm_out'):
            self.fields[f].input_formats = ['%H:%M', '%H:%M:%S']
        self.fields['workdays'] = self._build_workdays_field(required=True)
        if self.instance and self.instance.pk:
            self.fields['workdays'].initial = self.instance.workdays


class EmployeeScheduleForm(_WorkdaysMixin, BootstrapMixin, forms.ModelForm):
    class Meta:
        model = EmployeeSchedule
        fields = ('am_in', 'am_out', 'pm_in', 'pm_out', 'grace_period_minutes', 'workdays')
        widgets = {f: forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')
                   for f in ('am_in', 'am_out', 'pm_in', 'pm_out')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Every field optional — blank means "inherit global".
        for name, field in self.fields.items():
            field.required = False
            if name in ('am_in', 'am_out', 'pm_in', 'pm_out'):
                field.input_formats = ['%H:%M', '%H:%M:%S']
        self.fields['workdays'] = self._build_workdays_field(required=False)
        if self.instance and self.instance.pk and self.instance.workdays:
            self.fields['workdays'].initial = self.instance.workdays


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

        if log_type in ('OT_IN', 'OT_OUT'):
            from django.utils import timezone
            if not OTAuthorization.objects.filter(
                    employee=emp, date=timezone.localdate()).exists():
                raise forms.ValidationError(
                    'OT punches require an OT authorization for this employee '
                    'today (authorize it on the Overtime page first).')
        return cd
