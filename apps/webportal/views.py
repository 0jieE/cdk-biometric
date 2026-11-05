"""HTMX web admin portal (session auth, ADMIN role only)."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.core.paginator import Paginator
from django.db import models
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('apps.webportal')

from apps.api.selectors import build_daily_attendance, monthly_summary, weekly_hours
from apps.attendance.models import (
    Absence,
    AttendanceLog,
    Lates,
    OTAuthorization,
    Overtime,
    Undertime,
)
from apps.attendance.services import process_day
from apps.attendance.tasks import sync_attendance_task
from apps.devices.clients import get_device_client
from apps.devices.models import BiometricDevice
from apps.organization.models import (
    Department,
    Employee,
    EmployeeSchedule,
    GlobalSchedule,
    Holiday,
)
from apps.organization.schedule import get_effective_schedule
from apps.organization.trusted_time import trusted_localdate, trusted_now
from apps.reports.models import ReportJob
from apps.reports.tasks import generate_report_task

from .forms import (
    DepartmentForm,
    DeviceForm,
    EmployeeForm,
    EmployeeScheduleForm,
    EmployeeWeeklyHoursForm,
    GlobalScheduleForm,
    HolidayForm,
    ManualAttendanceForm,
    OTAuthorizationForm,
    ParttimeDefaultHoursForm,
    ReportForm,
)
from .utils import admin_required, is_htmx

User = get_user_model()
PAGE_SIZE = 15
DEFAULT_EMPLOYEE_PASSWORD = 'employee12345'


def _trigger(*events) -> HttpResponse:
    """Empty 204 response that fires client-side HTMX events."""
    resp = HttpResponse(status=204)
    resp['HX-Trigger'] = ','.join(events)
    return resp


def _parse_date(value, default=None):
    if not value:
        return default
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def login_view(request):
    if request.user.is_authenticated and getattr(request.user, 'is_admin', False):
        return redirect('webportal:dashboard')

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is None:
            messages.error(request, 'Invalid credentials.')
        elif not getattr(user, 'is_admin', False):
            messages.error(request, 'This portal is for admin accounts only.')
        else:
            login(request, user)
            return redirect('webportal:dashboard')

    return render(request, 'webportal/login.html')


def logout_view(request):
    logout(request)
    return redirect('webportal:login')


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@admin_required
def dashboard(request):
    import calendar as _cal

    from django.conf import settings

    today = trusted_localdate()
    day_start = timezone.make_aware(datetime.combine(today, time.min))
    day_end = day_start + timedelta(days=1)

    present = (
        AttendanceLog.objects.filter(
            log_type__in=['AM_IN', 'PM_IN', 'IN'],
            log_datetime__gte=day_start, log_datetime__lt=day_end)
        .values('employee').distinct().count()
    )

    month_start = today.replace(day=1)
    dept_lost, peak = _lost_time_by_department(month_start, today)
    # Four evenly-spaced axis labels, largest first.
    gridlines = [round(peak * f / 4) for f in (4, 3, 2, 1, 0)] if peak else [0]

    context = {
        'total_employees': Employee.objects.filter(is_active=True).count(),
        'present': present,
        'late': Lates.objects.filter(date=today).count(),
        'absent': Absence.objects.filter(date=today).count(),
        'today': today,

        # Calendar (current month) + holidays
        'weeks': _build_calendar(today.year, today.month),
        'month_name': _cal.month_name[today.month],
        'year': today.year,
        'weekday_labels': ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
        'upcoming_holidays': Holiday.objects.filter(date__gte=today).order_by('date')[:5],

        # Lost-time analytics for the current month
        'dept_lost': dept_lost,
        'gridlines': gridlines,
        'top_lost': _top_lost_employees(month_start, today, 5),
        'range_label': f'{month_start.strftime("%b %d")} – {today.strftime("%b %d")}',

        # Weather (Open-Meteo, client-side)
        'weather_lat': getattr(settings, 'WEATHER_LAT', 7.00833),
        'weather_lon': getattr(settings, 'WEATHER_LON', 125.08944),
    }
    return render(request, 'webportal/dashboard.html', context)


@admin_required
def dashboard_active_employees(request):
    employees = Employee.objects.filter(is_active=True).select_related(
        'department').order_by('department__name', 'employee_no')
    return render(request, 'webportal/partials/active_employees_modal.html',
                  {'employees': employees})


def _device_online(device):
    """Is this device reachable right now? quick_check(), not test_connection():
    the status badges poll every 30s, so this needs a real short timeout, not
    the full protocol handshake (see ZKDeviceClient.quick_check for why
    test_connection() alone can't be trusted to return quickly)."""
    try:
        return get_device_client(
            ip=device.ip_address, port=device.port).quick_check(timeout=2)
    except Exception:
        return False


@admin_required
def dashboard_device_status(request):
    device = BiometricDevice.objects.filter(is_active=True).first()
    online = device is not None and _device_online(device)
    return render(request, 'webportal/partials/dashboard_device_status.html',
                  {'device': device, 'online': online})


@admin_required
def dashboard_recent(request):
    logs = (
        AttendanceLog.objects.select_related('employee', 'device')
        .order_by('-log_datetime')[:5]
    )
    return render(request, 'webportal/partials/recent_punches.html', {'logs': logs})


@admin_required
def sync_now(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    # Run inline so the admin sees the result immediately (works without a worker).
    result = sync_attendance_task.apply().get()
    return render(request, 'webportal/partials/sync_result.html', {'result': result})


# ---------------------------------------------------------------------------
# Attendance logs
# ---------------------------------------------------------------------------
def _attendance_rows(start, end, department_id=None, employee_id=None, status=None,
                     is_fulltime=None):
    employees = Employee.objects.filter(is_active=True).select_related(
        'department', 'schedule_override')
    if is_fulltime is not None:
        employees = employees.filter(is_fulltime=is_fulltime)
    if department_id:
        employees = employees.filter(department_id=department_id)
    if employee_id:
        employees = employees.filter(id=employee_id)

    rows = []
    for emp in employees:
        for rec in build_daily_attendance(emp, start, end):
            if status and rec['day_status'] != status:
                continue
            rows.append({'employee': emp, **rec})
    rows.sort(key=lambda r: (r['date'], r['employee'].employee_no), reverse=True)
    return rows


def _attendance_filters(request):
    # NB: use date_start / date_end (NOT start/end) — DataTables reserves the
    # `start` and `length` GET params for server-side paging.
    today = trusted_localdate()
    return {
        'start': _parse_date(request.GET.get('date_start'), today - timedelta(days=7)),
        'end': _parse_date(request.GET.get('date_end'), today),
        'department': request.GET.get('department') or None,
        'employee': request.GET.get('employee') or None,
        'status': request.GET.get('status') or None,
    }


@admin_required
def attendance(request):
    return render(request, 'webportal/attendance.html', {
        'departments': Department.objects.filter(is_active=True),
        'employees': Employee.objects.filter(is_active=True),
        'statuses': ['PRESENT', 'LATE', 'HALF_DAY', 'ABSENT', 'INCOMPLETE'],
        'filters': _attendance_filters(request),
    })


@admin_required
def attendance_weekly(request):
    """Part-time tab: each part-timer's weeks in the filtered range, hours
    rendered vs. their weekly requirement."""
    f = _attendance_filters(request)
    employees = Employee.objects.filter(is_active=True, is_fulltime=False).select_related(
        'department', 'schedule_override').order_by('last_name', 'first_name')
    if f['department']:
        employees = employees.filter(department_id=f['department'])
    if f['employee']:
        employees = employees.filter(id=f['employee'])

    today = trusted_localdate()
    rows = [row for emp in employees
            for row in _weekly_rows(emp, f['start'], f['end'], today)]
    rows.sort(key=lambda r: (r['week_start'], r['employee'].last_name), reverse=True)
    return render(request, 'webportal/partials/attendance_weekly.html', {'rows': rows})


def _weekly_rows(employee, start, end, today):
    """weekly_hours() weeks, formatted for attendance_weekly.html."""
    rows = []
    for w in weekly_hours(employee, start, end, today=today):
        pct = min(100, round(w['worked_minutes'] / w['required_minutes'] * 100)) \
            if w['required_minutes'] else 100
        rows.append({'employee': employee, **w, 'pct': pct,
                     'worked': _hm(w['worked_minutes']),
                     'required': _hm(w['required_minutes']),
                     'remaining': _hm(w['remaining_minutes']),
                     'gap': _week_gap(w)})
    return rows


def _week_gap(week):
    """What a part-time week still lacks: '2h 30m', '1 day' or '2h 30m & 1 day'."""
    parts = []
    if week['remaining_minutes']:
        parts.append(_hm(week['remaining_minutes']))
    if week['remaining_days']:
        n = week['remaining_days']
        parts.append(f'{n} day' if n == 1 else f'{n} days')
    return ' & '.join(parts)


def _fmt_t(dt):
    return timezone.localtime(dt).strftime('%H:%M') if dt else '—'


def _hm(minutes):
    """450 -> '7h 30m', 480 -> '8h'."""
    h, m = divmod(int(minutes), 60)
    return f'{h}h {m:02d}m' if m else f'{h}h'


def _status_badge(status):
    m = {'PRESENT': ('s-present', 'PRESENT'), 'LATE': ('s-late', 'LATE'),
         'HALF_DAY': ('s-half', 'HALF DAY'), 'ABSENT': ('s-absent', 'ABSENT'),
         'INCOMPLETE': ('s-half', 'INCOMPLETE'), 'PENDING': ('s-neutral', 'IN PROGRESS')}
    cls, label = m.get(status, ('s-neutral', status))
    return f'<span class="badge-status {cls}">{label}</span>'


def _event_note(r):
    """Event-day credit (special holiday, attendance required) beside the status."""
    from django.utils.html import escape
    if not r.get('event'):
        return ''
    return (f' <i class="bi bi-calendar-event text-muted" '
            f'title="Whole day credited: {escape(r["event"])}"></i>')


def _who_cell(emp):
    return f'{emp.full_name} <span class="text-muted small">({emp.employee_no})</span>'


def _fulltime_cells(r):
    am, pm = r['am'], r['pm']
    return [r['date'].strftime('%b %d, %Y'), _who_cell(r['employee']),
            _fmt_t(am['in']), _fmt_t(am['out']), _fmt_t(pm['in']), _fmt_t(pm['out']),
            _status_badge(r['day_status']) + _event_note(r), str(r['late_minutes']),
            str(r['undertime_minutes']), str(r['absent_minutes']), str(r['lost_minutes']),
            str(r['overtime_minutes'])]


def _parttime_cells(r):
    incomplete = (f'<span class="badge-status s-absent">{r["missing"]} missing</span>'
                  if r.get('incomplete') else '—')
    return [r['date'].strftime('%b %d, %Y'), _who_cell(r['employee']),
            _fmt_t(r['in']), _fmt_t(r['out']), _hm(r['worked_minutes']),
            _status_badge(r['day_status']), incomplete]


@admin_required
def attendance_data(request):
    """DataTables server-side endpoint. ?emp_type=FULL_TIME|PART_TIME selects the
    tab (and its column shape)."""
    from django.http import JsonResponse

    f = _attendance_filters(request)
    is_ft = request.GET.get('emp_type') != 'PART_TIME'   # default: full-time tab
    rows = _attendance_rows(f['start'], f['end'], f['department'], f['employee'],
                            f['status'], is_fulltime=is_ft)
    total = len(rows)

    search = (request.GET.get('search[value]') or '').strip().lower()
    if search:
        rows = [r for r in rows
                if search in r['employee'].full_name.lower()
                or search in r['employee'].employee_no.lower()
                or search in r['day_status'].lower()]
    filtered = len(rows)

    keymap = {'0': lambda r: r['date'], '1': lambda r: r['employee'].employee_no}
    col = request.GET.get('order[0][column]')
    if col in keymap:
        rows.sort(key=keymap[col], reverse=(request.GET.get('order[0][dir]', 'desc') == 'desc'))

    start = int(request.GET.get('start', 0) or 0)
    length = int(request.GET.get('length', 10) or 10)
    page_rows = rows if length == -1 else rows[start:start + length]

    builder = _fulltime_cells if is_ft else _parttime_cells
    return JsonResponse({
        'draw': int(request.GET.get('draw', 1) or 1),
        'recordsTotal': total,
        'recordsFiltered': filtered,
        'data': [builder(r) for r in page_rows],
    })


# ---------------------------------------------------------------------------
# Tardiness
# ---------------------------------------------------------------------------
_EMP_FIELDS = ('employee', 'employee__first_name', 'employee__last_name',
               'employee__employee_no', 'employee__department__name')


def _lost_time_by_employee(late_qs, under_qs, absence_qs):
    """Merge per-employee lateness, undertime and absences into one lost-time
    table.

    lost time = minutes late + minutes undertime + minutes absent (each missed
    session's scheduled length).
    """
    agg = {}

    def bucket(row):
        return agg.setdefault(row['employee'], {
            'name': f"{row['employee__first_name']} {row['employee__last_name']}".strip(),
            'no': row['employee__employee_no'],
            'dept': row['employee__department__name'],
            'late_count': 0, 'late_minutes': 0,
            'under_count': 0, 'under_minutes': 0,
            'absent_count': 0, 'absent_minutes': 0,
        })

    # NB: .order_by() clears the querysets' explicit '-date' ordering. Django adds
    # explicitly-ordered fields to the GROUP BY, which would silently group by
    # (employee, date) and undercount each employee to a single day's total.
    for row in (late_qs.order_by().values(*_EMP_FIELDS)
                .annotate(c=models.Count('id'), m=models.Sum('minutes_late'))):
        b = bucket(row)
        b['late_count'], b['late_minutes'] = row['c'], row['m'] or 0

    for row in (under_qs.order_by().values(*_EMP_FIELDS)
                .annotate(c=models.Count('id'), m=models.Sum('minutes_undertime'))):
        b = bucket(row)
        b['under_count'], b['under_minutes'] = row['c'], row['m'] or 0

    for row in (absence_qs.order_by().values(*_EMP_FIELDS)
                .annotate(c=models.Count('id'), m=models.Sum('minutes_absent'))):
        b = bucket(row)
        b['absent_count'], b['absent_minutes'] = row['c'], row['m'] or 0

    rows = list(agg.values())
    for b in rows:
        b['lost_minutes'] = b['late_minutes'] + b['under_minutes'] + b['absent_minutes']
        b['lost_hours'] = round(b['lost_minutes'] / 60, 1)
    rows.sort(key=lambda b: -b['lost_minutes'])
    return rows


def _minutes_by(model, field, key, start, end):
    """{key value: summed ``field``} for ``model`` rows dated in [start, end]."""
    # .order_by() keeps the GROUP BY to ``key`` alone (see _lost_time_by_employee).
    return {r[key]: r['m'] or 0 for r in
            model.objects.filter(date__gte=start, date__lte=end).order_by()
            .values(key).annotate(m=models.Sum(field))}


def _lost_time_by_department(start, end):
    """Lost time (late + undertime + absent) summed per department, for the bar
    chart."""
    key = 'employee__department'
    late = _minutes_by(Lates, 'minutes_late', key, start, end)
    under = _minutes_by(Undertime, 'minutes_undertime', key, start, end)
    absent = _minutes_by(Absence, 'minutes_absent', key, start, end)

    rows = []
    for d in Department.objects.filter(is_active=True).order_by('name'):
        lm, um, am = late.get(d.id, 0), under.get(d.id, 0), absent.get(d.id, 0)
        rows.append({'name': d.name, 'code': d.code, 'color': d.avatar_color,
                     'late': lm, 'under': um, 'absent': am, 'total': lm + um + am})
    rows.sort(key=lambda r: -r['total'])
    peak = max([r['total'] for r in rows], default=0)
    for r in rows:
        r['pct'] = round(r['total'] / peak * 100) if peak else 0
    return rows, peak


def _top_lost_employees(start, end, limit=5):
    """The ``limit`` employees with the most lost time in the range."""
    late = _minutes_by(Lates, 'minutes_late', 'employee', start, end)
    under = _minutes_by(Undertime, 'minutes_undertime', 'employee', start, end)
    absent = _minutes_by(Absence, 'minutes_absent', 'employee', start, end)

    emps = {e.id: e for e in Employee.objects.filter(
        id__in=set(late) | set(under) | set(absent)).select_related('department')}
    rows = [{'employee': e,
             'late': late.get(eid, 0), 'under': under.get(eid, 0),
             'absent': absent.get(eid, 0),
             'total': late.get(eid, 0) + under.get(eid, 0) + absent.get(eid, 0)}
            for eid, e in emps.items()]
    rows.sort(key=lambda r: -r['total'])
    rows = rows[:limit]
    peak = rows[0]['total'] if rows else 0
    for r in rows:
        r['pct'] = round(r['total'] / peak * 100) if peak else 0
        r['hours'] = round(r['total'] / 60, 1)
    return rows


@admin_required
def lates(request):
    today = trusted_localdate()
    start = _parse_date(request.GET.get('start'), today.replace(day=1))
    end = _parse_date(request.GET.get('end'), today)
    dept_id = request.GET.get('department') or None

    late_qs = Lates.objects.select_related('employee', 'employee__department').filter(
        date__gte=start, date__lte=end).order_by('-date')
    under_qs = Undertime.objects.select_related('employee', 'employee__department').filter(
        date__gte=start, date__lte=end).order_by('-date')
    absence_qs = Absence.objects.filter(date__gte=start, date__lte=end)
    if dept_id:
        late_qs = late_qs.filter(employee__department_id=dept_id)
        under_qs = under_qs.filter(employee__department_id=dept_id)
        absence_qs = absence_qs.filter(employee__department_id=dept_id)

    per_employee = _lost_time_by_employee(late_qs, under_qs, absence_qs)
    totals = {
        'late': sum(b['late_minutes'] for b in per_employee),
        'under': sum(b['under_minutes'] for b in per_employee),
        'absent': sum(b['absent_minutes'] for b in per_employee),
    }
    totals['lost'] = totals['late'] + totals['under'] + totals['absent']

    context = {
        'rows': late_qs,
        'undertime_rows': under_qs,
        'per_employee': per_employee,
        'totals': totals,
        'departments': Department.objects.filter(is_active=True),
        'filters': {'start': start, 'end': end, 'department': dept_id},
    }
    template = 'webportal/partials/lates_table.html' if is_htmx(request) \
        else 'webportal/lates.html'
    return render(request, template, context)


# ---------------------------------------------------------------------------
# Absences
# ---------------------------------------------------------------------------
@admin_required
def absences(request):
    today = trusted_localdate()
    start = _parse_date(request.GET.get('start'), today.replace(day=1))
    end = _parse_date(request.GET.get('end'), today)
    dept_id = request.GET.get('department') or None

    qs = Absence.objects.select_related('employee', 'employee__department').filter(
        date__gte=start, date__lte=end).order_by('-date')
    if dept_id:
        qs = qs.filter(employee__department_id=dept_id)

    context = {
        'rows': qs,
        'departments': Department.objects.filter(is_active=True),
        'filters': {'start': start, 'end': end, 'department': dept_id},
    }
    template = 'webportal/partials/absences_table.html' if is_htmx(request) \
        else 'webportal/absences.html'
    return render(request, template, context)


# ---------------------------------------------------------------------------
# Employees CRUD
# ---------------------------------------------------------------------------
@admin_required
def employees(request):
    # Full list; the card grid is filtered client-side (search / dept / type).
    rows = Employee.objects.select_related('department').order_by('employee_no')
    if is_htmx(request):
        return render(request, 'webportal/partials/employees_cards.html', {'rows': rows})
    return render(request, 'webportal/employees.html', {
        'rows': rows,
        'departments': Department.objects.filter(is_active=True),
    })


@admin_required
def employee_form(request, pk=None):
    instance = get_object_or_404(Employee, pk=pk) if pk else None
    if request.method == 'POST':
        form = EmployeeForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            employee = form.save()
            messages.success(request, 'Employee saved.')
            if instance is None:
                _provision_employee_account(employee)
                # Step 2, in the same modal: their schedule (full-time) or
                # weekly hours (part-time), skippable to keep the defaults.
                resp = _schedule_setup_form(request, employee, onboarding=True)
                resp['HX-Trigger'] = 'refreshList'
                return resp
            # Editing from the profile page should refresh that page, not a list.
            return _trigger('refreshList', 'refreshProfile', 'closeModal')
        return render(request, 'webportal/partials/employee_form.html',
                      {'form': form, 'instance': instance})

    form = EmployeeForm(instance=instance)
    return render(request, 'webportal/partials/employee_form.html',
                  {'form': form, 'instance': instance})


def _provision_employee_account(employee):
    """Create the linked EMPLOYEE User. Schedule comes from the GlobalSchedule
    (or a per-employee EmployeeSchedule override set on the Schedule page)."""
    if User.objects.filter(employee=employee).exists():
        return                       # already has a login (possibly renamed by them)
    # Default login = the employee number. If someone already holds that name (an
    # employee may rename themselves from the mobile app), take the next free one
    # rather than silently creating nothing and leaving the new hire unable to sign in.
    base = employee.employee_no.lower()
    username, n = base, 2
    while User.objects.filter(username__iexact=username).exists():
        username = f'{base}-{n}'
        n += 1
    user = User.objects.create(
        username=username,
        email=f'{base}@ckc.edu.ph',
        role=User.Roles.EMPLOYEE,
        employee=employee,
        first_name=employee.first_name,
        last_name=employee.last_name,
    )
    user.set_password(DEFAULT_EMPLOYEE_PASSWORD)
    user.save()


def _set_employee_active(employee, active: bool):
    """Deactivating is one switch, not two: the employee and their login move
    together. A deactivated employee keeps every past record (nothing is
    deleted), but stops counting as an employee, can't sign in to the mobile
    app, and - since ``ingest_punches`` only recognises active employees by
    biometric_id - their device punches are no longer recorded either."""
    employee.is_active = active
    employee.save(update_fields=['is_active'])
    linked = User.objects.filter(employee=employee).first()
    if linked:
        linked.is_active = active
        linked.save(update_fields=['is_active'])


@admin_required
def employee_deactivate(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    _set_employee_active(employee, False)
    messages.success(request, f'{employee.full_name} deactivated.')
    return _trigger('refreshList', 'refreshProfile', 'closeModal')


@admin_required
def employee_reactivate(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    _set_employee_active(employee, True)
    messages.success(request, f'{employee.full_name} reactivated.')
    return _trigger('refreshList', 'refreshProfile', 'closeModal')


# --- Employee profile + performance analytics -------------------------------
def _month_sequence(today, n):
    """Last ``n`` (year, month) pairs ending with the current month, oldest first."""
    y, m, seq = today.year, today.month, []
    for _ in range(n):
        seq.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(seq))


def _employee_performance(employee):
    """Assemble the analytics shown on the profile page.

    A 30-day window drives the headline rates; a 6-month series drives the trend
    bars. The day-by-day records load separately (employee_records).
    """
    today = trusted_localdate()
    window_start = today - timedelta(days=29)
    records = build_daily_attendance(employee, window_start, today)

    def count(status):
        return sum(1 for r in records if r['day_status'] == status)

    present, late = count('PRESENT'), count('LATE')
    half, absent = count('HALF_DAY'), count('ABSENT')
    counted = present + late + half + absent
    attended = present + late + half
    ot_minutes = sum(r.get('overtime_minutes', 0) for r in records)
    late_minutes = sum(r.get('late_minutes', 0) for r in records)
    undertime_minutes = sum(r.get('undertime_minutes', 0) for r in records)
    absent_minutes = sum(r.get('absent_minutes', 0) for r in records)
    lost_minutes = late_minutes + undertime_minutes + absent_minutes

    attendance_rate = round(attended / counted * 100) if counted else 0
    punctuality_rate = round(present / (present + late) * 100) if (present + late) else 100

    trend = []
    for (y, m) in _month_sequence(today, 6):
        s = monthly_summary(employee, y, m)
        c = s['present'] + s['late'] + s['half_day'] + s['absent'] + s['incomplete']
        rate = round((s['present'] + s['late'] + s['half_day']) / c * 100) if c else 0
        label = datetime(y, m, 1).strftime('%b')
        trend.append({'label': label, 'rate': rate, 'summary': s})

    parttime = {}
    if not employee.is_fulltime:
        weeks = weekly_hours(employee, window_start, today, today=today)
        done = [w for w in weeks if w['status'] != 'IN_PROGRESS']
        met = sum(1 for w in done if w['status'] == 'MET')
        this_week = weeks[-1] if weeks else None
        parttime = {
            'pt_weeks_met': met, 'pt_weeks_done': len(done),
            'pt_met_rate': round(met / len(done) * 100) if done else 100,
            'pt_this_week': this_week,
            'pt_this_week_worked': _hm(this_week['worked_minutes']) if this_week else '0h',
            'pt_this_week_required': _hm(this_week['required_minutes']) if this_week else '',
            'pt_this_week_pct': (min(100, round(this_week['worked_minutes']
                                               / this_week['required_minutes'] * 100))
                                 if this_week and this_week['required_minutes'] else 0),
            'incomplete_days': count('INCOMPLETE'),
            'worked_hours': round(sum(r.get('worked_minutes', 0) for r in records) / 60, 1),
        }

    return {
        **parttime,
        'window_days': 30,
        'present_days': present, 'late_days': late,
        'half_days': half, 'absent_days': absent,
        'counted_days': counted,
        'attendance_rate': attendance_rate,
        'punctuality_rate': punctuality_rate,
        'ot_hours': round(ot_minutes / 60, 1),
        'ot_minutes': ot_minutes,
        'late_minutes': late_minutes,
        'undertime_minutes': undertime_minutes,
        'absent_minutes': absent_minutes,
        'lost_minutes': lost_minutes,
        'lost_hours': round(lost_minutes / 60, 1),
        'trend': trend,
    }


@admin_required
def employee_detail(request, pk):
    employee = get_object_or_404(Employee.objects.select_related('department'), pk=pk)
    context = {
        'employee': employee,
        'account': User.objects.filter(employee=employee).first(),
        **_employee_performance(employee),
    }
    # refreshProfile re-fetches only the inner region after an edit / account change.
    template = 'webportal/partials/employee_profile_body.html' if is_htmx(request) \
        else 'webportal/employee_detail.html'
    return render(request, template, context)


_RECORD_STATUSES = [('PRESENT', 'Present'), ('LATE', 'Late'), ('HALF_DAY', 'Half day'),
                    ('ABSENT', 'Absent'), ('INCOMPLETE', 'Incomplete'),
                    ('PENDING', 'In progress')]
_RECORD_TOTALS = ('late_minutes', 'undertime_minutes', 'absent_minutes', 'lost_minutes',
                  'overtime_minutes', 'worked_minutes')


@admin_required
def employee_records(request, pk):
    """The profile's day-by-day attendance records for a date range (default: the
    same 30 days as the analytics), filterable by status, with totals."""
    employee = get_object_or_404(Employee, pk=pk)
    today = trusted_localdate()
    end = _parse_date(request.GET.get('end'), today)
    start = _parse_date(request.GET.get('start'), end - timedelta(days=29))
    if start > end:
        start, end = end, start
    start = max(start, end - timedelta(days=365))      # keep one page bounded
    status = request.GET.get('status') or ''

    days = list(reversed(build_daily_attendance(employee, start, end)))
    counts = {s: 0 for s, _ in _RECORD_STATUSES}
    for r in days:
        counts[r['day_status']] = counts.get(r['day_status'], 0) + 1
    records = [r for r in days if r['day_status'] == status] if status else days
    for r in records:
        r['worked'] = _hm(r.get('worked_minutes', 0))
    totals = {k: sum(r.get(k, 0) for r in records) for k in _RECORD_TOTALS}
    totals['worked'] = _hm(totals['worked_minutes'])

    return render(request, 'webportal/partials/employee_records.html', {
        'employee': employee, 'records': records, 'totals': totals,
        'start': start, 'end': end, 'status': status,
        'statuses': _RECORD_STATUSES,
        'status_counts': [(s, label, counts[s]) for s, label in _RECORD_STATUSES if counts[s]],
        'weeks': ([] if employee.is_fulltime
                  else list(reversed(_weekly_rows(employee, start, end, today)))),
    })


@admin_required
def employee_account(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    account = User.objects.filter(employee=employee).first()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create' and account is None:
            _provision_employee_account(employee)
            messages.success(request, f'Login created for {employee.full_name}.')
        elif account and action == 'reset':
            account.set_password(DEFAULT_EMPLOYEE_PASSWORD)
            account.save()
            messages.success(request, f'Password reset to the default for {employee.full_name}.')
        return _trigger('refreshProfile', 'closeModal')

    return render(request, 'webportal/partials/employee_account.html', {
        'employee': employee, 'account': account,
        'default_password': DEFAULT_EMPLOYEE_PASSWORD,
    })


# ---------------------------------------------------------------------------
# Departments CRUD
# ---------------------------------------------------------------------------
@admin_required
def departments(request):
    # Prefetch a handful of active members per department for the avatar stack.
    rows = Department.objects.annotate(
        emp_count=models.Count('employees', filter=models.Q(employees__is_active=True)),
    ).prefetch_related(
        models.Prefetch(
            'employees',
            queryset=Employee.objects.filter(is_active=True).order_by('employee_no'),
            to_attr='member_preview'),
    ).order_by('code')
    if is_htmx(request):
        return render(request, 'webportal/partials/departments_cards.html', {'rows': rows})
    return render(request, 'webportal/departments.html', {'rows': rows})


@admin_required
def department_detail(request, pk):
    department = get_object_or_404(Department, pk=pk)
    employees_qs = Employee.objects.select_related('department').filter(
        department=department).order_by('employee_no')
    context = {
        'department': department,
        'rows': employees_qs,
        'active_count': employees_qs.filter(is_active=True).count(),
        'total_count': employees_qs.count(),
    }
    template = 'webportal/partials/department_body.html' if is_htmx(request) \
        else 'webportal/department_detail.html'
    return render(request, template, context)


@admin_required
def department_form(request, pk=None):
    instance = get_object_or_404(Department, pk=pk) if pk else None
    if request.method == 'POST':
        form = DepartmentForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, 'Department saved.')
            return _trigger('refreshList', 'refreshProfile', 'closeModal')
        return render(request, 'webportal/partials/department_form.html',
                      {'form': form, 'instance': instance})
    form = DepartmentForm(instance=instance)
    return render(request, 'webportal/partials/department_form.html',
                  {'form': form, 'instance': instance})


# ---------------------------------------------------------------------------
# Holidays CRUD
# ---------------------------------------------------------------------------
def _build_calendar(year, month):
    """Weeks of day-cells (Sun→Sat) for the month grid, each cell carrying any
    holidays that fall on it (recurring ones matched by month/day)."""
    import calendar as _cal
    from datetime import date as _date

    cal = _cal.Calendar(firstweekday=6)  # 6 = Sunday
    weeks = cal.monthdatescalendar(year, month)
    grid_start, grid_end = weeks[0][0], weeks[-1][-1]

    exact, recurring = {}, {}
    for h in Holiday.objects.all():
        if h.is_recurring:
            recurring.setdefault((h.date.month, h.date.day), []).append(h)
        if grid_start <= h.date <= grid_end:
            exact.setdefault(h.date, []).append(h)

    today = trusted_localdate()
    out_weeks = []
    for week in weeks:
        cells = []
        for d in week:
            hols = list(exact.get(d, []))
            for h in recurring.get((d.month, d.day), []):
                if h.date != d:  # avoid double-listing a recurring one on its own year
                    hols.append(h)
            cells.append({
                'date': d, 'day': d.day,
                'in_month': d.month == month,
                'is_today': d == today,
                'is_weekend': d.weekday() >= 5,
                'holidays': hols,
            })
        out_weeks.append(cells)
    return out_weeks


@admin_required
def holidays(request):
    import calendar as _cal

    today = trusted_localdate()
    try:
        year = int(request.GET.get('year') or today.year)
        month = int(request.GET.get('month') or today.month)
    except (TypeError, ValueError):
        year, month = today.year, today.month
    month = min(12, max(1, month))

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    weeks = _build_calendar(year, month)

    # Flat, de-duplicated list of this month's holidays for the overview panel.
    month_holidays, seen = [], set()
    for week in weeks:
        for c in week:
            if not c['in_month']:
                continue
            for h in c['holidays']:
                if h.pk in seen:
                    continue
                seen.add(h.pk)
                month_holidays.append({
                    'id': h.pk, 'date': c['date'], 'day': c['date'].day,
                    'name': h.name, 'color': h.color,
                    'type_label': h.get_type_display(),
                    'required_label': h.required_label,
                })
    month_holidays.sort(key=lambda x: x['day'])

    from django.conf import settings

    context = {
        'weeks': weeks, 'today': today,
        'month_holidays': month_holidays,
        'year': year, 'month': month,
        'month_name': _cal.month_name[month],
        'weekday_labels': ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
        'prev_y': prev_y, 'prev_m': prev_m,
        'next_y': next_y, 'next_m': next_m,
        # Weather (Open-Meteo, client-side). Defaults to Kidapawan City,
        # Cotabato (per Open-Meteo geocoding: 7.00833 N, 125.08944 E).
        'weather_lat': getattr(settings, 'WEATHER_LAT', 7.00833),
        'weather_lon': getattr(settings, 'WEATHER_LON', 125.08944),
    }
    template = 'webportal/partials/holiday_calendar.html' if is_htmx(request) \
        else 'webportal/holidays.html'
    return render(request, template, context)


def _reprocess_dates(*dates):
    """Re-run process_day for everyone on these dates, so a holiday added,
    changed or removed is reflected in the stored Absence/Lates rows straight
    away. Future dates are skipped - nothing is judged before it happens."""
    today = trusted_localdate()
    targets = sorted({d for d in dates if d and d <= today})
    if not targets:
        return
    for emp in Employee.objects.filter(is_active=True).select_related('schedule_override'):
        for d in targets:
            process_day(emp, d)


@admin_required
def holiday_form(request, pk=None):
    instance = get_object_or_404(Holiday, pk=pk) if pk else None
    if request.method == 'POST':
        old_date = instance.date if instance else None
        form = HolidayForm(request.POST, instance=instance)
        if form.is_valid():
            holiday = form.save()
            _reprocess_dates(old_date, holiday.date)
            messages.success(request, 'Holiday saved.')
            return _trigger('refreshList', 'closeModal')
        return render(request, 'webportal/partials/holiday_form.html',
                      {'form': form, 'instance': instance})
    # Pre-fill the date when the admin clicked a calendar day.
    initial = {}
    if not instance:
        clicked = _parse_date(request.GET.get('date'))
        if clicked:
            initial['date'] = clicked
    form = HolidayForm(instance=instance, initial=initial)
    return render(request, 'webportal/partials/holiday_form.html',
                  {'form': form, 'instance': instance})


@admin_required
def holiday_delete(request, pk):
    holiday = get_object_or_404(Holiday, pk=pk)
    day = holiday.date
    holiday.delete()
    _reprocess_dates(day)
    messages.success(request, 'Holiday removed.')
    return _trigger('refreshList', 'closeModal')


@admin_required
def holiday_list(request):
    """Full table of every holiday (the calendar's 'See all' destination)."""
    rows = Holiday.objects.order_by('-date')
    if is_htmx(request):
        return render(request, 'webportal/partials/holidays_table.html', {'rows': rows})
    return render(request, 'webportal/holidays_all.html', {'rows': rows})


@admin_required
def holiday_import(request):
    """Seed Philippine national holidays for a year. Idempotent — existing dates
    are skipped. Tries the configured API, falls back to the generator."""
    from .ph_holidays import fetch_from_api, philippine_holidays

    if request.method != 'POST':
        return HttpResponse(status=405)
    today = trusted_localdate()
    try:
        year = int(request.GET.get('year') or today.year)
    except (TypeError, ValueError):
        year = today.year

    items = fetch_from_api(year) or philippine_holidays(year)
    existing = set(Holiday.objects.filter(
        date__year=year).values_list('date', flat=True))
    created = 0
    for h in items:
        if h['date'] in existing:
            continue
        Holiday.objects.create(
            date=h['date'], name=h['name'], type=h['type'],
            color=h.get('color', '#DC2626'))
        created += 1
    messages.success(
        request,
        f'Imported {created} Philippine holiday(s) for {year}'
        + (f'; {len(items) - created} already existed.' if created < len(items) else '.'))
    return _trigger('refreshList')


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
@admin_required
def devices(request):
    context = {'devices': BiometricDevice.objects.order_by('name')}
    template = 'webportal/partials/devices_cards.html' if is_htmx(request) \
        else 'webportal/devices.html'
    return render(request, template, context)


@admin_required
def device_form(request, pk=None):
    instance = get_object_or_404(BiometricDevice, pk=pk) if pk else None
    if request.method == 'POST':
        form = DeviceForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, 'Device saved.')
            return _trigger('refreshList', 'closeModal')
        return render(request, 'webportal/partials/device_form.html',
                      {'form': form, 'instance': instance})
    form = DeviceForm(instance=instance)
    return render(request, 'webportal/partials/device_form.html',
                  {'form': form, 'instance': instance})


@admin_required
def device_sync(request, pk):
    device = get_object_or_404(BiometricDevice, pk=pk)
    result = sync_attendance_task.apply().get()
    resp = render(request, 'webportal/partials/sync_result.html',
                  {'result': result, 'device': device})
    resp['HX-Trigger'] = 'refreshList'           # the card's "Last synced" moved
    return resp


@admin_required
def device_status(request, pk):
    """One card's Connection row. Each card loads (and re-polls) its own, so an
    offline device's timeout never holds up the list or the other cards."""
    device = get_object_or_404(BiometricDevice, pk=pk)
    return render(request, 'webportal/partials/device_status.html',
                  {'device': device, 'online': _device_online(device),
                   'checked_at': timezone.localtime()})


@admin_required
def device_test(request, pk):
    device = get_object_or_404(BiometricDevice, pk=pk)
    try:
        # This device's own address - not necessarily whichever one is "active" -
        # so Test always checks the device the admin actually clicked.
        ok = get_device_client(ip=device.ip_address, port=device.port).test_connection()
    except Exception:
        ok = False
    return render(request, 'webportal/partials/device_test.html',
                  {'ok': ok, 'device': device})


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def _report_jobs_filters(request):
    today = trusted_localdate()
    return {
        'q': (request.GET.get('q') or '').strip(),
        'start': _parse_date(request.GET.get('start'), today - timedelta(days=30)),
        'end': _parse_date(request.GET.get('end'), today),
    }


def _recent_jobs(filters=None):
    """Report jobs matching ``filters`` (search text + a date range on when the
    job was requested), each tagged with the employee it was for. Employee
    labels are resolved in ONE query, not one per row — the list re-polls
    every 3 seconds."""
    filters = filters or {}
    start, end = filters.get('start'), filters.get('end')
    qs = ReportJob.objects.select_related('requested_by')
    # Explicit UTC datetime bounds, not a `created_at__date` lookup: on MySQL
    # that lookup needs CONVERT_TZ, which silently returns NULL (matching
    # nothing) unless the server's timezone tables happen to be loaded.
    if start:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(start, time.min)))
    if end:
        qs = qs.filter(created_at__lt=timezone.make_aware(datetime.combine(end, time.min))
                       + timedelta(days=1))

    q = filters.get('q')
    if q:
        emp_ids = list(Employee.objects.filter(
            models.Q(first_name__icontains=q) | models.Q(last_name__icontains=q) |
            models.Q(employee_no__icontains=q),
        ).values_list('id', flat=True))
        match = (models.Q(fmt__icontains=q) | models.Q(status__icontains=q) |
                 models.Q(report_type__icontains=q))
        for emp_id in emp_ids:
            match |= models.Q(params__employee=emp_id)
        qs = qs.filter(match)

    jobs = list(qs.order_by('-created_at')[:200])
    ids = {int(j.params['employee']) for j in jobs if (j.params or {}).get('employee')}
    labels = {e.pk: f'{e.employee_no} · {e.full_name}'
              for e in Employee.objects.filter(pk__in=ids)}
    for job in jobs:
        params = job.params or {}
        emp_id = params.get('employee')
        job.employee_label = labels.get(int(emp_id), '') if emp_id else ''
        if params.get('start'):
            job.detail = f"{params['start']} to {params.get('end', '')}"
    return jobs


@admin_required
def reports(request):
    if request.method == 'POST':
        form = ReportForm(request.POST)
        if form.is_valid():
            job = ReportJob.objects.create(
                report_type=ReportJob.ReportType.ATTENDANCE,
                fmt=form.cleaned_data['fmt'],
                params=form.to_params(),
                requested_by=request.user,
                status=ReportJob.Status.PENDING,
            )
            # Enqueue async; fall back to inline if no broker/worker is up.
            try:
                generate_report_task.delay(job.id)
            except Exception:
                generate_report_task.apply(args=[job.id])
            messages.success(request, 'Report queued.')
            return _trigger('refreshJobs')
        return render(request, 'webportal/partials/report_form.html', {'form': form})

    filters = _report_jobs_filters(request)
    context = {
        'form': ReportForm(),
        'jobs': _recent_jobs(filters),
        'job_filters': filters,
    }
    return render(request, 'webportal/reports.html', context)


@admin_required
def report_jobs(request):
    filters = _report_jobs_filters(request)
    return render(request, 'webportal/partials/report_jobs.html',
                  {'jobs': _recent_jobs(filters), 'job_filters': filters})


@admin_required
def export_report(request):
    """Synchronous export/download for list-page 'Export' buttons.

    Reads report_type + fmt + filter params straight from the query string and
    streams the built file back as an attachment.
    """
    from apps.reports.generators import build_report

    report_type = request.GET.get('report_type', 'DAILY_ATTENDANCE')
    fmt = request.GET.get('fmt', 'XLSX')
    params = {
        k: request.GET.get(k)
        for k in ('date', 'month', 'start', 'end', 'department', 'employee')
        if request.GET.get(k)
    }
    filename, content = build_report(report_type, fmt, params)

    content_types = {
        'XLSX': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'PDF': 'application/pdf',
    }
    resp = HttpResponse(content, content_type=content_types.get(fmt, 'application/octet-stream'))
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


# ---------------------------------------------------------------------------
# Schedule settings (GlobalSchedule + per-employee overrides)
# ---------------------------------------------------------------------------
@admin_required
def schedule_settings(request):
    schedule = GlobalSchedule.load()
    if request.method == 'POST':
        form = GlobalScheduleForm(request.POST, instance=schedule)
        if form.is_valid():
            form.save()
            messages.success(request, 'Global schedule saved.')
            return redirect('webportal:schedule')
        return render(request, 'webportal/schedule.html', _schedule_context(global_form=form))
    return render(request, 'webportal/schedule.html', _schedule_context())


@admin_required
def schedule_parttime_default(request):
    """Save the part-time default weekly hours and minimum days (the Part-time
    tab's form)."""
    if request.method != 'POST':
        return redirect(f"{reverse('webportal:schedule')}?tab=parttime")
    form = ParttimeDefaultHoursForm(request.POST, instance=GlobalSchedule.load())
    if form.is_valid():
        form.save()
        messages.success(request, 'Part-time weekly requirement saved.')
        return redirect(f"{reverse('webportal:schedule')}?tab=parttime")
    return render(request, 'webportal/schedule.html',
                  _schedule_context(parttime_form=form, tab='parttime'))


def _schedule_context(global_form=None, parttime_form=None, tab=None):
    schedule = GlobalSchedule.load()
    return {'global_form': global_form or GlobalScheduleForm(instance=schedule),
            'parttime_form': parttime_form or ParttimeDefaultHoursForm(instance=schedule),
            'global_schedule': schedule, 'tab': tab}


@admin_required
def employee_schedule_list(request):
    # ?type=PART_TIME lists part-timers with their weekly hours; anything
    # else lists full-timers with their schedule overrides.
    parttime = request.GET.get('type') == 'PART_TIME'
    employees = Employee.objects.filter(is_active=True, is_fulltime=not parttime).select_related(
        'department', 'schedule_override').order_by('employee_no')
    if parttime:
        default = GlobalSchedule.load().parttime_weekly_hours
        for e in employees:
            own = getattr(getattr(e, 'schedule_override', None), 'weekly_hours', None)
            e.weekly_hours, e.has_own_hours = (own, True) if own is not None else (default, False)
        return render(request, 'webportal/partials/schedule_parttime_list.html',
                      {'employees': employees})
    return render(request, 'webportal/partials/schedule_employee_list.html',
                  {'employees': employees})


_OVERRIDE_FIELDS = ('am_in', 'am_out', 'pm_in', 'pm_out', 'midpoint',
                    'grace_period_minutes', 'workdays', 'weekly_hours')


def _override_for(employee):
    """The employee's override row, or an unsaved one. Opening a form must not
    create a row: an empty override still reads as 'Custom schedule'."""
    return (EmployeeSchedule.objects.filter(employee=employee).first()
            or EmployeeSchedule(employee=employee))


def _save_override(form):
    """Save, but drop the row again if every field ended up blank (= inherit)."""
    override = form.save()
    if all(getattr(override, f) in (None, '', []) for f in _OVERRIDE_FIELDS):
        override.delete()


def _schedule_setup_form(request, employee, form=None, onboarding=False):
    """The modal for an employee's schedule: weekly hours for a part-timer,
    the AM/PM override for a full-timer. ``onboarding`` is the step right after
    adding them, which offers to skip (= keep the defaults)."""
    override = _override_for(employee)
    if employee.is_fulltime:
        return render(request, 'webportal/partials/employee_schedule_form.html', {
            'form': form or EmployeeScheduleForm(instance=override), 'employee': employee,
            'effective': get_effective_schedule(employee), 'onboarding': onboarding,
            'action_url': reverse('webportal:employee_schedule', args=[employee.id])})
    return render(request, 'webportal/partials/employee_weekly_hours_form.html', {
        'form': form or EmployeeWeeklyHoursForm(instance=override), 'employee': employee,
        'default_hours': GlobalSchedule.load().parttime_weekly_hours, 'onboarding': onboarding,
        'action_url': reverse('webportal:employee_weekly_hours', args=[employee.id])})


def _onboarding(request):
    """A re-render after a validation error stays on the 'Step 2' framing."""
    return request.POST.get('onboarding') == '1'


@admin_required
def employee_weekly_hours_form(request, pk):
    employee = get_object_or_404(Employee, pk=pk, is_fulltime=False)
    if request.method == 'POST':
        form = EmployeeWeeklyHoursForm(request.POST, instance=_override_for(employee))
        if form.is_valid():
            _save_override(form)
            messages.success(request, f'Weekly hours saved for {employee.full_name}.')
            return _trigger('refreshList', 'closeModal')
        return _schedule_setup_form(request, employee, form, _onboarding(request))
    return _schedule_setup_form(request, employee)


@admin_required
def employee_weekly_hours_clear(request, pk):
    """Back to the default hours - only weekly_hours, the rest of any override stays."""
    employee = get_object_or_404(Employee, pk=pk, is_fulltime=False)
    EmployeeSchedule.objects.filter(employee=employee).update(weekly_hours=None)
    messages.success(request, f'{employee.full_name} now follows the default weekly hours.')
    return _trigger('refreshList')


@admin_required
def employee_schedule_form(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    if request.method == 'POST':
        form = EmployeeScheduleForm(request.POST, instance=_override_for(employee))
        if form.is_valid():
            _save_override(form)
            messages.success(request, f'Override saved for {employee.full_name}.')
            return _trigger('refreshList', 'closeModal')
        return _schedule_setup_form(request, employee, form, _onboarding(request))
    return _schedule_setup_form(request, employee)


@admin_required
def employee_schedule_clear(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    EmployeeSchedule.objects.filter(employee=employee).delete()
    messages.success(request, f'Override cleared for {employee.full_name} (inherits global).')
    return _trigger('refreshList')


# ---------------------------------------------------------------------------
# Overtime authorization (the admin-granted privilege)
# ---------------------------------------------------------------------------
@admin_required
def overtime(request):
    if request.method == 'POST':
        form = OTAuthorizationForm(request.POST)
        if form.is_valid():
            auth = form.save(commit=False)
            auth.approved_by = request.user
            auth.save()
            # Recompute so OT accrues immediately if the punches exist.
            process_day(auth.employee, auth.date)
            messages.success(request, 'Overtime authorized.')
            return _trigger('refreshList', 'closeModal')
        return render(request, 'webportal/partials/overtime_form.html', {'form': form})

    if is_htmx(request):
        return render(request, 'webportal/partials/overtime_table.html', _overtime_context(request))
    return render(request, 'webportal/overtime.html', _overtime_context(request))


def _overtime_context(request):
    rows = list(OTAuthorization.objects.select_related('employee', 'approved_by').order_by('-date'))
    ot_by_key = {(o.employee_id, o.date): o.minutes for o in Overtime.objects.all()}
    for auth in rows:
        auth.computed_minutes = ot_by_key.get((auth.employee_id, auth.date))
    return {'rows': rows}


@admin_required
def overtime_form(request):
    return render(request, 'webportal/partials/overtime_form.html',
                  {'form': OTAuthorizationForm()})


@admin_required
def overtime_revoke(request, pk):
    auth = get_object_or_404(OTAuthorization, pk=pk)
    employee, day = auth.employee, auth.date
    auth.delete()
    process_day(employee, day)  # OT recomputes to zero (no authorization)
    messages.success(request, 'Authorization revoked; overtime recomputed.')
    return _trigger('refreshList')


# ---------------------------------------------------------------------------
# Manual attendance (brownout fallback) — strictly ADMIN
# ---------------------------------------------------------------------------
def _minutes(t):
    return t.hour * 60 + t.minute


def _manual_context(form, result):
    emps = (Employee.objects.filter(is_active=True).select_related('schedule_override')
            .order_by('first_name', 'last_name'))
    data = []
    for e in emps:
        row = {'id': e.id, 'name': e.full_name, 'no': e.employee_no,
               'ft': e.is_fulltime, 'initials': e.initials, 'color': e.avatar_color,
               'photo': e.photo.url if e.photo else ''}
        if e.is_fulltime:
            # Minutes since midnight, so the terminal can open/close AM/PM
            # In and Out by the employee's own schedule.
            s = get_effective_schedule(e)
            row['sched'] = {'am_in': _minutes(s.am_in), 'am_out': _minutes(s.am_out),
                            'pm_in': _minutes(s.pm_in), 'pm_out': _minutes(s.pm_out),
                            'midpoint': _minutes(s.midpoint)}
        data.append(row)
    recent = (AttendanceLog.objects.select_related('employee')
              .order_by('-log_datetime')[:8])
    # The server's local wall clock as if it were UTC: the terminal's clock and
    # its open/closed rules follow the server, not the browser's own clock.
    local_now = timezone.localtime(trusted_now()).replace(tzinfo=None)
    server_ms = int((local_now - datetime(1970, 1, 1)).total_seconds() * 1000)
    return {'form': form, 'result': result, 'employees_data': data,
            'recent_punches': recent, 'server_ms': server_ms}


@admin_required
def manual_attendance(request):
    result = None
    if request.method == 'POST':
        form = ManualAttendanceForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            now = timezone.localtime(trusted_now())  # date + time default to now
            log, _created = AttendanceLog.objects.update_or_create(
                employee=cd['employee'], log_datetime=now, log_type=cd['log_type'],
                defaults={'source': AttendanceLog.Source.MANUAL,
                          'created_by': request.user})
            # Immediate per-punch notification (idempotent by the log link).
            from apps.notifications.services import notify_for_log
            notify_for_log(log)
            outcome = process_day(cd['employee'], now.date())
            messages.success(
                request,
                f"{cd['log_type']} recorded for {cd['employee'].full_name} at "
                f"{now.strftime('%I:%M %p')}; day status: {outcome['status']}.")
            result = {'employee': cd['employee'], 'time': now,
                      'log_type': cd['log_type'], 'status': outcome['status']}
            form = ManualAttendanceForm()
        return render(request, 'webportal/manual_attendance.html',
                      _manual_context(form, result))
    return render(request, 'webportal/manual_attendance.html',
                  _manual_context(ManualAttendanceForm(), None))


# ---------------------------------------------------------------------------
# Live logs — PUBLIC kiosk (no login)
# ---------------------------------------------------------------------------
def _live_enabled():
    from django.conf import settings
    return getattr(settings, 'LIVE_LOGS_PUBLIC', True)


LIVE_FEED_LATEST = 10


def live_logs(request):
    if not _live_enabled():
        raise Http404()
    return render(request, 'webportal/live.html', {})


def live_logs_feed(request):
    """Today's punches. Default: latest 10 (auto-refreshing).
    ``?all=1`` browses every punch recorded today."""
    if not _live_enabled():
        raise Http404()

    today = trusted_localdate()
    day_start = timezone.make_aware(datetime.combine(today, time.min))
    day_end = day_start + timedelta(days=1)

    qs = (AttendanceLog.objects
          .select_related('employee', 'employee__department')
          .filter(log_datetime__gte=day_start, log_datetime__lt=day_end)
          .order_by('-log_datetime'))

    total = qs.count()
    show_all = request.GET.get('all') == '1'
    logs = qs if show_all else qs[:LIVE_FEED_LATEST]

    return render(request, 'webportal/partials/live_feed.html', {
        'logs': logs, 'total': total, 'show_all': show_all,
        'latest_n': LIVE_FEED_LATEST,
    })


# Seconds between heartbeat comments on the live-log SSE stream: frequent
# enough to stay well under nginx's proxy_read_timeout and gunicorn's worker
# timeout (both default to well over a minute), so the connection is never
# killed as "stalled" while it's actually just waiting for the next punch.
LIVE_STREAM_HEARTBEAT_SECONDS = 15


def live_logs_stream(request):
    """Server-Sent Events stream: pushes 'refresh' the instant a new punch is
    committed (see apps.attendance.realtime.publish_new_log), so the /live/
    kiosk page updates immediately instead of waiting for a poll.

    Ties up one app-server worker for as long as the tab stays open — fine
    for a handful of kiosk displays, which is what this page is for.
    """
    if not _live_enabled():
        raise Http404()

    from apps.attendance.realtime import LIVE_PUNCH_CHANNEL

    def event_stream():
        yield ': connected\n\n'  # flush headers immediately; browser sees the stream is live
        try:
            import redis
            client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
            pubsub = client.pubsub()
            pubsub.subscribe(LIVE_PUNCH_CHANNEL)
        except Exception:
            logger.warning('live-log SSE: could not reach Redis; client will '
                            'fall back to its own periodic poll.')
            return
        try:
            while True:
                message = pubsub.get_message(
                    timeout=LIVE_STREAM_HEARTBEAT_SECONDS, ignore_subscribe_messages=True)
                if message is None:
                    yield ': ping\n\n'  # heartbeat — keeps proxies/timeouts happy
                else:
                    yield 'data: refresh\n\n'
        except GeneratorExit:
            raise
        except Exception:
            logger.warning('live-log SSE: stream loop ended unexpectedly.')
        finally:
            try:
                pubsub.close()
                client.close()
            except Exception:
                pass

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'  # tell nginx not to buffer this response
    return response


# ---------------------------------------------------------------------------
# How it works (help page)
# ---------------------------------------------------------------------------
_WEEKDAY_NAMES = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def _plus(t, minutes):
    """time + minutes (same day)."""
    return (datetime.combine(datetime.min, t) + timedelta(minutes=minutes)).time()


@admin_required
def how_it_works(request):
    """Plain-language guide to how attendance is recorded and calculated. The
    numbers come from the live schedule settings, so the examples always match
    what the system actually does."""
    from apps.attendance.services import DUPLICATE_PUNCH_WINDOW, LUNCH_DUPLICATE_WINDOW

    g = GlobalSchedule.load()

    def span(start, end):
        return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)

    am_minutes, pm_minutes = span(g.am_in, g.am_out), span(g.pm_in, g.pm_out)
    late_by, under_by = 7, 15
    example = {
        'arrived': _plus(g.am_in, g.grace_period_minutes + late_by),
        'late': late_by,
        'left': _plus(g.pm_out, -under_by),
        'under': under_by,
        'day1_lost': late_by + under_by,
        'pm_absent': pm_minutes,
        'total': _hm(late_by + under_by + pm_minutes),
    }
    # Sample days of scans, as (time, what it becomes, why).
    scans = [
        (_plus(g.am_in, -6), 'AM In', 'first scan of the morning'),
        (_plus(g.am_in, -4), 'ignored', 'pressed again 2 minutes later: a duplicate'),
        (_plus(g.am_out, 2), 'AM Out', 'last scan before the midpoint'),
        (_plus(g.pm_in, -3), 'PM In', 'first scan after the midpoint'),
        (_plus(g.pm_out, 4), 'PM Out', 'last scan of the afternoon'),
    ]
    forgot = [
        (_plus(g.am_in, -2), 'AM In', 'first scan of the morning'),
        (_plus(g.midpoint, max(1, span(g.midpoint, g.pm_in) // 3)), 'AM Out',
         'no AM Out yet, so this lunch scan closes the morning'),
        (_plus(g.pm_in, -4), 'PM In', 'the next scan starts the afternoon'),
        (_plus(g.pm_out, 1), 'PM Out', 'last scan of the afternoon'),
    ]
    weekly = g.parttime_weekly_hours.normalize()
    return render(request, 'webportal/how_it_works.html', {
        'g': g, 'scans': scans, 'forgot': forgot,
        'workdays': ', '.join(_WEEKDAY_NAMES[d] for d in sorted(g.workdays)),
        'am_hours': _hm(am_minutes), 'pm_hours': _hm(pm_minutes),
        'am_minutes': am_minutes, 'pm_minutes': pm_minutes,
        'day_hours': _hm(am_minutes + pm_minutes),
        'grace_until': _plus(g.am_in, g.grace_period_minutes),
        'example': example,
        'weekly_hours': f'{weekly:f}',
        'sync_minutes': settings.SYNC_INTERVAL_MINUTES,
        'duplicate_minutes': int(DUPLICATE_PUNCH_WINDOW.total_seconds() // 60),
        'lunch_minutes': int(LUNCH_DUPLICATE_WINDOW.total_seconds() // 60),
    })
