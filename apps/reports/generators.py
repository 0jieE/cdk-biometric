"""Report data builders + Excel/PDF renderers.

A generator turns (report_type, params) into a titled table (columns + rows),
which is then rendered to XLSX (openpyxl) or PDF (xhtml2pdf). Both renderers
consume the same table, so the two formats never drift apart.
"""

from __future__ import annotations

import calendar
from datetime import date as date_cls
from datetime import datetime
from io import BytesIO

from django.conf import settings
from django.db.models import Sum
from django.template.loader import render_to_string
from django.utils import timezone

from apps.api.selectors import build_daily_attendance, monthly_summary
from apps.attendance.models import Absence, Lates
from apps.organization.models import Department, Employee

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


def _employees(department_id=None):
    qs = Employee.objects.filter(is_active=True).select_related(
        'department', 'schedule_override')
    if department_id:
        qs = qs.filter(department_id=department_id)
    return qs.order_by('employee_no')


def _fmt_time(dt):
    return timezone.localtime(dt).strftime('%H:%M') if dt else '—'


def _dept_label(department_id):
    if not department_id:
        return 'All departments'
    dept = Department.objects.filter(pk=department_id).first()
    return dept.name if dept else 'All departments'


# ---------------------------------------------------------------------------
# Data builders -> {title, columns, rows}
# ---------------------------------------------------------------------------
def _daily_attendance(params) -> dict:
    day = _parse_date(params.get('date'), timezone.localdate())
    dept_id = params.get('department') or None
    columns = ['Employee No', 'Name', 'Department', 'Type',
               'AM In', 'AM Out', 'PM In', 'PM Out',
               'Day Status', 'Minutes Late', 'OT Minutes']
    rows = []
    for emp in _employees(dept_id):
        recs = build_daily_attendance(emp, day, day)
        rec = recs[0] if recs else None
        if rec is None:
            rows.append([emp.employee_no, emp.full_name, emp.department.name,
                         emp.employment_type, '—', '—', '—', '—', 'REST', 0, 0])
        elif emp.is_fulltime:
            am, pm = rec['am'], rec['pm']
            minutes = am['minutes_late'] + pm['minutes_late']
            rows.append([
                emp.employee_no, emp.full_name, emp.department.name, 'FULL_TIME',
                _fmt_time(am['in']), _fmt_time(am['out']),
                _fmt_time(pm['in']), _fmt_time(pm['out']),
                rec['day_status'], minutes, rec['overtime_minutes'],
            ])
        else:
            note = f" ({rec['missing']} missing)" if rec.get('incomplete') else ''
            rows.append([
                emp.employee_no, emp.full_name, emp.department.name, 'PART_TIME',
                _fmt_time(rec['in']), _fmt_time(rec['out']), '—', '—',
                rec['day_status'] + note, 0, 0,
            ])
    return {
        'title': f'Daily Attendance — {day:%Y-%m-%d} ({_dept_label(dept_id)})',
        'columns': columns, 'rows': rows,
    }


def _monthly_summary(params) -> dict:
    month = params.get('month') or timezone.localdate().strftime('%Y-%m')
    year, mon = (int(p) for p in month.split('-'))
    dept_id = params.get('department') or None
    first = date_cls(year, mon, 1)
    last = date_cls(year, mon, calendar.monthrange(year, mon)[1])

    columns = ['Employee No', 'Name', 'Department', 'Type', 'Present', 'Late',
               'Half-day', 'Absent', 'Total Minutes Late', 'Authorized OT Minutes']
    rows = []
    for emp in _employees(dept_id):
        s = monthly_summary(emp, year, mon)
        total_late = (
            Lates.objects.filter(employee=emp, date__gte=first, date__lte=last)
            .aggregate(t=Sum('minutes_late'))['t'] or 0
        )
        rows.append([
            emp.employee_no, emp.full_name, emp.department.name, emp.employment_type,
            s['present'], s['late'], s['half_day'], s['absent'],
            total_late, s['overtime_minutes'],
        ])
    return {
        'title': f'Monthly Summary — {month} ({_dept_label(dept_id)})',
        'columns': columns, 'rows': rows,
    }


def _tardiness(params) -> dict:
    start = _parse_date(params.get('start'), timezone.localdate().replace(day=1))
    end = _parse_date(params.get('end'), timezone.localdate())
    dept_id = params.get('department') or None
    qs = Lates.objects.filter(date__gte=start, date__lte=end).select_related(
        'employee', 'employee__department').order_by('date', 'employee__employee_no')
    if dept_id:
        qs = qs.filter(employee__department_id=dept_id)

    columns = ['Date', 'Employee No', 'Name', 'Department', 'Session', 'Minutes Late']
    rows = [
        [l.date.strftime('%Y-%m-%d'), l.employee.employee_no,
         l.employee.full_name, l.employee.department.name, l.session, l.minutes_late]
        for l in qs
    ]
    return {
        'title': f'Tardiness — {start:%Y-%m-%d} to {end:%Y-%m-%d} ({_dept_label(dept_id)})',
        'columns': columns, 'rows': rows,
    }


def _absence(params) -> dict:
    start = _parse_date(params.get('start'), timezone.localdate().replace(day=1))
    end = _parse_date(params.get('end'), timezone.localdate())
    dept_id = params.get('department') or None
    qs = Absence.objects.filter(date__gte=start, date__lte=end).select_related(
        'employee', 'employee__department').order_by('date', 'employee__employee_no')
    if dept_id:
        qs = qs.filter(employee__department_id=dept_id)

    columns = ['Date', 'Employee No', 'Name', 'Department', 'Session', 'Reason', 'Incomplete']
    rows = [
        [a.date.strftime('%Y-%m-%d'), a.employee.employee_no,
         a.employee.full_name, a.employee.department.name, a.session,
         a.get_reason_display(), 'Yes' if a.incomplete else 'No']
        for a in qs
    ]
    return {
        'title': f'Absences — {start:%Y-%m-%d} to {end:%Y-%m-%d} ({_dept_label(dept_id)})',
        'columns': columns, 'rows': rows,
    }


_BUILDERS = {
    'DAILY_ATTENDANCE': _daily_attendance,
    'MONTHLY_SUMMARY': _monthly_summary,
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

    if fmt == 'XLSX':
        return f'{report_type.lower()}_{stamp}.xlsx', render_xlsx(table)
    if fmt == 'PDF':
        return f'{report_type.lower()}_{stamp}.pdf', render_pdf(table)
    raise ValueError(f'Unknown fmt: {fmt}')
