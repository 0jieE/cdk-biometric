"""Report data builders + Excel/PDF renderers.

A generator turns (report_type, params) into a titled table (columns + rows),
which is then rendered to XLSX (openpyxl) or PDF (xhtml2pdf). Both renderers
consume the same table, so the two formats never drift apart.
"""

from __future__ import annotations

import re
from datetime import date as date_cls
from datetime import datetime, timedelta
from io import BytesIO

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from apps.api.selectors import (
    HOLIDAY_OFF,
    WEEK_IN_PROGRESS,
    WEEK_MET,
    WEEK_SHORT,
    build_daily_attendance,
    weekly_hours,
)
from apps.attendance.models import Absence, Lates
from apps.organization.models import Department, Employee
from apps.organization.schedule import get_effective_schedule

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


def _filename_part(text: str) -> str:
    """'Ma. Peña Santos' -> 'Ma_Peña_Santos': word characters joined by '_',
    so the name survives as a filename on any OS."""
    return re.sub(r'[^\w-]+', '_', text).strip('_') or 'employee'


def _minutes_label(minutes: int) -> str:
    """'2,400 min (40.0 h)' - minutes plus the same in hours, for summaries."""
    return f'{minutes:,} min ({minutes / 60:.1f} h)'


_WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _schedule_label(sched, fulltime: bool) -> str:
    if fulltime:
        days = ', '.join(_WEEKDAYS[d] for d in sorted(sched.workdays))
        return (f'AM {sched.am_in:%H:%M}–{sched.am_out:%H:%M} · '
                f'PM {sched.pm_in:%H:%M}–{sched.pm_out:%H:%M} · {days}')
    return f'{sched.weekly_hours.normalize():f} hours per week (Mon–Sun), any days'


def _hours(minutes: int) -> float:
    """Minutes as decimal hours for a table cell: 450 -> 7.5."""
    return round(minutes / 60, 2)


_WEEK_LABELS = {WEEK_MET: 'Met', WEEK_IN_PROGRESS: 'In progress'}   # SHORT says by how much


def _week_gap(week) -> str:
    """What a short week lacked: '2.5 h', '1 day' or '2.5 h & 1 day'."""
    parts = []
    if week['remaining_minutes']:
        parts.append(f'{_hours(week["remaining_minutes"]):g} h')
    if week['remaining_days']:
        n = week['remaining_days']
        parts.append(f'{n} day' if n == 1 else f'{n} days')
    return ' & '.join(parts)


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
_STATUS_LABELS = {'HALF_DAY': 'HALF DAY', 'PENDING': 'IN PROGRESS'}  # others print as-is


def _attendance(params) -> dict:
    """The Attendance page's table over a date range, for one employee.

    Same columns and cell values as the page (full-time: AM/PM punches plus late /
    undertime / absent / lost / OT minutes; part-time: in / out / incomplete), newest day
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
                   'Day Status', 'Late (min)', 'Undertime (min)', 'Absent (min)',
                   'Lost (min)', 'OT (min)']
    else:
        columns = ['Date', 'Employee', 'In', 'Out', 'Hours', 'Day Status', 'Incomplete']

    who = f'{emp.full_name} ({emp.employee_no})'
    records = build_daily_attendance(emp, start, end)
    rows = []
    for rec in reversed(records):   # newest first
        head = [rec['date'].strftime('%b %d, %Y'), who]
        status = _STATUS_LABELS.get(rec['day_status'], rec['day_status'])
        if rec.get('event'):          # holiday off / event day credited present
            status = f"{status} · {rec['event']}"
        if fulltime:
            am, pm = rec['am'], rec['pm']
            rows.append(head + [
                _fmt_time(am['in']), _fmt_time(am['out']),
                _fmt_time(pm['in']), _fmt_time(pm['out']), status,
                rec['late_minutes'], rec['undertime_minutes'], rec['absent_minutes'],
                rec['lost_minutes'], rec['overtime_minutes']])
        else:
            incomplete = f"{rec['missing']} missing" if rec.get('incomplete') else '—'
            rows.append(head + [_fmt_time(rec['in']), _fmt_time(rec['out']),
                                _hours(rec['worked_minutes']), status, incomplete])

    def count(status):
        return sum(1 for r in records if r['day_status'] == status)

    def total(key):
        return sum(r.get(key, 0) for r in records)

    days = f'{len(records)} day{"" if len(records) == 1 else "s"}'
    sched = get_effective_schedule(emp)
    weeks_table = None
    if fulltime:
        absent_min, lost_min = total('absent_minutes'), total('lost_minutes')
        late_min, under_min, ot_min = (total('late_minutes'), total('undertime_minutes'),
                                       total('overtime_minutes'))
        holidays_off = sum(1 for r in records if r.get('event', '').endswith(HOLIDAY_OFF))
        present = count('PRESENT')
        present_label = (f'{present} (incl. {holidays_off} holiday'
                         f'{"" if holidays_off == 1 else "s"})' if holidays_off else present)
        day_counts = [('Present', present_label), ('Late', count('LATE')),
                      ('Half day', count('HALF_DAY')), ('Absent', count('ABSENT'))]
        totals = ['TOTAL', '', '', '', '', '', days,
                  late_min, under_min, absent_min, lost_min, ot_min]
        # (label, value, is_total) - totals are emphasised in the PDF.
        time_group = ('Time', [
            ('Late', _minutes_label(late_min), False),
            ('Undertime', _minutes_label(under_min), False),
            ('Absent', _minutes_label(absent_min), False),
            ('Lost time (late + undertime + absent)', _minutes_label(lost_min), True),
            ('Overtime', _minutes_label(ot_min), False)])
    else:
        worked_min = total('worked_minutes')
        day_counts = [('Days worked', count('PRESENT')),
                      ('Incomplete (0 h)', count('INCOMPLETE'))]
        totals = ['TOTAL', '', '', '', _hours(worked_min), days,
                  f'{count("INCOMPLETE")} incomplete']
        weeks = weekly_hours(emp, start, end)
        met = sum(1 for w in weeks if w['status'] == WEEK_MET)
        short = sum(1 for w in weeks if w['status'] == WEEK_SHORT)
        time_group = ('Hours', [
            ('Weekly requirement', f'{sched.weekly_hours.normalize():f} h / week', False),
            ('Minimum days', f'{sched.weekly_min_days} days / week', False),
            ('Weeks met', f'{met} of {len(weeks)}', False),
            ('Weeks short', str(short), False),
            ('Total hours rendered', _minutes_label(worked_min), True)])
        weeks_table = {
            'heading': 'Weekly Hours',
            'columns': ['Week (Mon–Sun)', 'Days', 'Rendered (h)', 'Required (h)', 'Status'],
            'rows': [[f'{w["week_start"]:%b %d} – {w["week_end"]:%b %d, %Y}',
                      w['days_worked'], _hours(w['worked_minutes']),
                      _hours(w['required_minutes']),
                      (f'Short by {_week_gap(w)}'
                       if w['status'] == WEEK_SHORT else _WEEK_LABELS[w['status']])]
                     for w in weeks],
        }
    if count('PENDING'):
        day_counts.append(('In progress', count('PENDING')))
    day_items = [(k, str(v), False) for k, v in day_counts]
    day_items.append(('Total days', str(len(records)), True))
    period_days = (end - start).days + 1
    return {
        'title': (f'Attendance — {who} · {emp.department.name} · '
                  f'{start:%Y-%m-%d} to {end:%Y-%m-%d}'),
        'doc_title': 'Employee Attendance Report',
        'columns': columns, 'rows': rows,
        'totals': totals if rows else None,
        # The PDF's details block names the employee once, so its table
        # drops the per-row Employee column.
        'subject': [
            ('Employee', emp.full_name),
            ('Employee No.', emp.employee_no),
            ('Department', emp.department.name),
            ('Employment type', 'Full-time' if fulltime else 'Part-time'),
            ('Schedule', _schedule_label(sched, fulltime)),
            ('Period covered', f'{start:%B %d, %Y} – {end:%B %d, %Y} '
                               f'({period_days} calendar day{"" if period_days == 1 else "s"})'),
        ],
        'summary': [('Days', day_items), time_group],
        'weeks_table': weeks_table,
        'signatory': emp.full_name,
        # Downloaded as e.g. Maria_Santos_2026-10-15_to_2026-11-30.pdf
        'file_stem': f'{_filename_part(emp.full_name)}_{start:%Y-%m-%d}_to_{end:%Y-%m-%d}',
    }


def _tardiness(params) -> dict:
    start = _parse_date(params.get('start'), timezone.localdate().replace(day=1))
    end = _parse_date(params.get('end'), timezone.localdate())
    dept_id = params.get('department') or None
    emp_id = _int_or_none(params.get('employee'))
    qs = Lates.objects.filter(date__gte=start, date__lte=end).select_related(
        'employee', 'employee__department').order_by('date', 'employee__employee_no')
    if emp_id:
        qs = qs.filter(employee_id=emp_id)    # one named employee: even if deactivated
    else:
        qs = qs.filter(employee__is_active=True)
        if dept_id:
            qs = qs.filter(employee__department_id=dept_id)

    columns = ['Date', 'Employee No', 'Name', 'Department', 'Session', 'Minutes Late']
    rows = [
        [l.date.strftime('%Y-%m-%d'), l.employee.employee_no,
         l.employee.full_name, l.employee.department.name, l.session, l.minutes_late]
        for l in qs
    ]
    return {
        'title': f'Tardiness — {start:%Y-%m-%d} to {end:%Y-%m-%d} ({_scope_label(dept_id, emp_id)})',
        'doc_title': 'Tardiness Report',
        'columns': columns, 'rows': rows,
        'totals': (['TOTAL', '', '', '', f'{len(rows)} record(s)', sum(r[5] for r in rows)]
                   if rows else None),
    }


def _absence(params) -> dict:
    start = _parse_date(params.get('start'), timezone.localdate().replace(day=1))
    end = _parse_date(params.get('end'), timezone.localdate())
    dept_id = params.get('department') or None
    emp_id = _int_or_none(params.get('employee'))
    qs = Absence.objects.filter(date__gte=start, date__lte=end).select_related(
        'employee', 'employee__department').order_by('date', 'employee__employee_no')
    if emp_id:
        qs = qs.filter(employee_id=emp_id)    # one named employee: even if deactivated
    else:
        qs = qs.filter(employee__is_active=True)
        if dept_id:
            qs = qs.filter(employee__department_id=dept_id)

    columns = ['Date', 'Employee No', 'Name', 'Department', 'Session', 'Reason', 'Incomplete',
               'Minutes Absent']
    rows = [
        [a.date.strftime('%Y-%m-%d'), a.employee.employee_no,
         a.employee.full_name, a.employee.department.name, a.session,
         a.get_reason_display(), 'Yes' if a.incomplete else 'No', a.minutes_absent]
        for a in qs
    ]
    return {
        'title': f'Absences — {start:%Y-%m-%d} to {end:%Y-%m-%d} ({_scope_label(dept_id, emp_id)})',
        'doc_title': 'Absence Report',
        'columns': columns, 'rows': rows,
        'totals': (['TOTAL', '', '', '', f'{len(rows)} session(s)', '', '',
                    sum(r[7] for r in rows)] if rows else None),
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

    totals = table.get('totals')
    if totals:
        ws.append(totals)
        for col_idx in range(1, len(totals) + 1):
            ws.cell(row=ws.max_row, column=col_idx).font = Font(bold=True)

    ws.freeze_panes = 'A2'

    # Auto width from the longest cell in each column.
    for col_idx, column in enumerate(table['columns'], start=1):
        width = len(str(column))
        for row in table['rows']:
            width = max(width, len(str(row[col_idx - 1])))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(width + 4, 50)

    weeks = table.get('weeks_table')
    if weeks:
        ws2 = wb.create_sheet(weeks['heading'])
        ws2.append(weeks['columns'])
        for col_idx in range(1, len(weeks['columns']) + 1):
            cell = ws2.cell(row=1, column=col_idx)
            cell.font, cell.fill = header_font, header_fill
            ws2.column_dimensions[get_column_letter(col_idx)].width = 22
        for row in weeks['rows']:
            ws2.append(row)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


_PDF_COL_WIDTHS = {'Date': '14%', 'Day Status': '11%'}


def _pdf_cells(row, keep):
    """Cells for the PDF: only the kept columns, numbers right-aligned with
    thousands separators (decimal hours to two places)."""
    def fmt(v):
        if isinstance(v, float):
            return f'{v:,.2f}'
        return f'{v:,}' if isinstance(v, int) else v

    return [{'v': fmt(v), 'num': isinstance(v, (int, float))}
            for i, v in enumerate(row) if i in keep]


def _span_blanks(cells):
    """Fold each blank cell into the one before it (as a colspan), so a totals
    row reads 'TOTAL' across the columns it has nothing to sum in."""
    merged = []
    for c in cells:
        if c['v'] == '' and merged:
            merged[-1]['span'] += 1
        else:
            merged.append({**c, 'span': 1})
    return merged


def render_pdf(table: dict) -> bytes:
    logo = settings.BASE_DIR / 'apps' / 'webportal' / 'static' / 'webportal' / 'img' / 'logo.png'
    columns = table['columns']
    keep = set(range(len(columns)))
    if table.get('subject') and 'Employee' in columns:
        keep.discard(columns.index('Employee'))   # named once in the details block
    totals = table.get('totals')
    subject = table.get('subject') or []
    html = render_to_string('reports/report.html', {
        'institution': getattr(settings, 'INSTITUTION_NAME', 'Institution'),
        'doc_title': table.get('doc_title') or table['title'],
        'title': table['title'],
        # Two label/value pairs per row keeps the details block compact.
        'subject_rows': [subject[i:i + 2] for i in range(0, len(subject), 2)],
        'summary': table.get('summary'),
        'signatory': table.get('signatory'),
        # (name, width): on a wide table, room for 'Oct 30, 2026' and
        # 'HALF DAY' on one line; the other columns share what's left.
        'columns': [(c, _PDF_COL_WIDTHS.get(c, '') if len(keep) > 6 else '')
                    for i, c in enumerate(columns) if i in keep],
        'rows': [_pdf_cells(r, keep) for r in table['rows']],
        'totals': _span_blanks(_pdf_cells(totals, keep)) if totals else None,
        'weeks_table': ({**weeks, 'rows': [_pdf_cells(r, set(range(len(r))))
                                           for r in weeks['rows']]}
                        if (weeks := table.get('weeks_table')) else None),
        'generated_at': timezone.localtime().strftime('%B %d, %Y, %I:%M %p'),
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
    # Per-employee reports are named for the employee and the period; the
    # rest by report type and generation time.
    base = table.get('file_stem') or (
        f"{report_type.lower()}_{timezone.localtime():%Y%m%d_%H%M%S}")

    if fmt == 'XLSX':
        return f'{base}.xlsx', render_xlsx(table)
    if fmt == 'PDF':
        return f'{base}.pdf', render_pdf(table)
    raise ValueError(f'Unknown fmt: {fmt}')
