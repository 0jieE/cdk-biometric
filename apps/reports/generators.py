"""Report data builders + Excel/PDF renderers.

A generator turns (report_type, params) into a titled table (columns + rows),
which is then rendered to XLSX (openpyxl) or PDF (xhtml2pdf). Both renderers
consume the same table, so the two formats never drift apart.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta
from io import BytesIO

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from apps.api.selectors import build_daily_attendance
from apps.attendance.models import Absence, Lates
from apps.organization.models import Department, Employee

# Longest date range a single report may cover.
MAX_RANGE_DAYS = 366

# --- xlsx styling ---
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# --- pdf ---
from xhtml2pdf import pisa


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _parse_date(value, default=None) -> date_cls | None:
    if not value:
        return default
    if isinstance(value, date_cls):
        return value
    return datetime.strptime(value, '%Y-%m-%d').date()


def _int_or_none(value):
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        raise ValueError(f'Expected a numeric id, got {value!r}')


def _fmt_time(dt):
    return timezone.localtime(dt).strftime('%H:%M') if dt else '—'


def _dept_label(department_id):
    if not department_id:
        return 'All departments'
    dept = Department.objects.filter(pk=department_id).first()
    return dept.name if dept else 'All departments'


def _scope_label(department_id, employee_id=None):
    """Title suffix: the chosen employee, else the department filter."""
    if employee_id:
        emp = Employee.objects.filter(pk=employee_id).first()
        if emp:
            return f'{emp.employee_no} {emp.full_name}'
    return _dept_label(department_id)


# ---------------------------------------------------------------------------
# Data builders -> {title, columns, rows}
# ---------------------------------------------------------------------------
_STATUS_LABELS = {'HALF_DAY': 'HALF DAY', 'PENDING': 'IN PROGRESS'}


def _attendance(params) -> dict:
    """The Attendance page's table over a date range, for one employee.

    Same columns and cell values as the page (full-time: AM/PM punches plus late /
    undertime / lost / OT minutes; part-time: in / out / incomplete), newest day
    first. The employee's own type decides which columns apply. An inactive
    employee can still be reported (e.g. someone who has left).
    """
    emp_id = _int_or_none(params.get('employee'))
    if emp_id is None:
        raise ValueError('The Attendance report needs an employee.')
    emp = Employee.objects.select_related('department', 'schedule_override').filter(
        pk=emp_id).first()
    if emp is None:
        raise ValueError(f'Employee {emp_id} not found.')

    today = timezone.localdate()
    start = _parse_date(params.get('start'), today - timedelta(days=7))
    end = _parse_date(params.get('end'), today)
    if start > end:
        raise ValueError('Start date must be on or before the end date.')
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f'Date range is too long (max {MAX_RANGE_DAYS} days).')

    fulltime = emp.is_fulltime
    if fulltime:
        columns = ['Date', 'Employee', 'AM In', 'AM Out', 'PM In', 'PM Out',
                   'Day Status', 'Late (min)', 'Undertime (min)', 'Lost (min)', 'OT (min)']
    else:
        columns = ['Date', 'Employee', 'In', 'Out', 'Day Status', 'Incomplete']

    who = f'{emp.full_name} ({emp.employee_no})'
    rows = []
    for rec in reversed(build_daily_attendance(emp, start, end)):   # newest first
        head = [rec['date'].strftime('%b %d, %Y'), who]
        status = _STATUS_LABELS.get(rec['day_status'], rec['day_status'])
        if fulltime:
            am, pm = rec['am'], rec['pm']
            rows.append(head + [
                _fmt_time(am['in']), _fmt_time(am['out']),
                _fmt_time(pm['in']), _fmt_time(pm['out']), status,
                rec['late_minutes'], rec['undertime_minutes'],
                rec['lost_minutes'], rec['overtime_minutes']])
        else:
            incomplete = f"{rec['missing']} missing" if rec.get('incomplete') else '—'
            rows.append(head + [_fmt_time(rec['in']), _fmt_time(rec['out']),
                                status, incomplete])

    return {
        'title': (f'Attendance — {who} · {emp.department.name} · '
                  f'{start:%Y-%m-%d} to {end:%Y-%m-%d}'),
        'columns': columns, 'rows': rows,
        'slug': emp.employee_no,   # goes into the downloaded filename
    }


def _tardiness(params) -> dict:
    start = _parse_date(params.get('start'), timezone.localdate().replace(day=1))
    end = _parse_date(params.get('end'), timezone.localdate())
    dept_id = params.get('department') or None
    emp_id = _int_or_none(params.get('employee'))
    qs = Lates.objects.filter(date__gte=start, date__lte=end).select_related(
        'employee', 'employee__department').order_by('date', 'employee__employee_no')
    if emp_id:
        qs = qs.filter(employee_id=emp_id)
    elif dept_id:
        qs = qs.filter(employee__department_id=dept_id)

    columns = ['Date', 'Employee No', 'Name', 'Department', 'Session', 'Minutes Late']
    rows = [
        [l.date.strftime('%Y-%m-%d'), l.employee.employee_no,
         l.employee.full_name, l.employee.department.name, l.session, l.minutes_late]
        for l in qs
    ]
    return {
        'title': f'Tardiness — {start:%Y-%m-%d} to {end:%Y-%m-%d} ({_scope_label(dept_id, emp_id)})',
        'columns': columns, 'rows': rows,
    }


def _absence(params) -> dict:
    start = _parse_date(params.get('start'), timezone.localdate().replace(day=1))
    end = _parse_date(params.get('end'), timezone.localdate())
    dept_id = params.get('department') or None
    emp_id = _int_or_none(params.get('employee'))
    qs = Absence.objects.filter(date__gte=start, date__lte=end).select_related(
        'employee', 'employee__department').order_by('date', 'employee__employee_no')
    if emp_id:
        qs = qs.filter(employee_id=emp_id)
    elif dept_id:
        qs = qs.filter(employee__department_id=dept_id)

    columns = ['Date', 'Employee No', 'Name', 'Department', 'Session', 'Reason', 'Incomplete']
    rows = [
        [a.date.strftime('%Y-%m-%d'), a.employee.employee_no,
         a.employee.full_name, a.employee.department.name, a.session,
         a.get_reason_display(), 'Yes' if a.incomplete else 'No']
        for a in qs
    ]
    return {
        'title': f'Absences — {start:%Y-%m-%d} to {end:%Y-%m-%d} ({_scope_label(dept_id, emp_id)})',
        'columns': columns, 'rows': rows,
    }


_BUILDERS = {
    'ATTENDANCE': _attendance,
    'TARDINESS': _tardiness,
    'ABSENCE': _absence,
}


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
def render_xlsx(table: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = 'Report'

    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='2C3E50')

    ws.append(table['columns'])
    for col_idx, _ in enumerate(table['columns'], start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')

    for row in table['rows']:
        ws.append(row)

    ws.freeze_panes = 'A2'

    # Auto width from the longest cell in each column.
    for col_idx, column in enumerate(table['columns'], start=1):
        width = len(str(column))
        for row in table['rows']:
            width = max(width, len(str(row[col_idx - 1])))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(width + 4, 50)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_pdf(table: dict) -> bytes:
    logo = settings.BASE_DIR / 'apps' / 'webportal' / 'static' / 'webportal' / 'img' / 'logo.png'
    html = render_to_string('reports/report.html', {
        'institution': getattr(settings, 'INSTITUTION_NAME', 'Institution'),
        'title': table['title'],
        'columns': table['columns'],
        'rows': table['rows'],
        'generated_at': timezone.localtime().strftime('%Y-%m-%d %H:%M'),
        'logo_path': str(logo) if logo.exists() else '',
    })
    buf = BytesIO()
    result = pisa.CreatePDF(src=html, dest=buf)
    if result.err:
        raise RuntimeError('xhtml2pdf failed to render the report')
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_report(report_type: str, fmt: str, params: dict) -> tuple[str, bytes]:
    """Return (filename, file_bytes) for the requested report."""
    try:
        builder = _BUILDERS[report_type]
    except KeyError:
        raise ValueError(f'Unknown report_type: {report_type}')

    table = builder(params or {})
    stamp = timezone.localtime().strftime('%Y%m%d_%H%M%S')
    # Per-employee reports carry the employee number in the filename.
    base = report_type.lower() + (f"_{table['slug']}" if table.get('slug') else '')

    if fmt == 'XLSX':
        return f'{base}_{stamp}.xlsx', render_xlsx(table)
    if fmt == 'PDF':
        return f'{base}_{stamp}.pdf', render_pdf(table)
    raise ValueError(f'Unknown fmt: {fmt}')
