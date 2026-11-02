from datetime import time

from django.db import migrations, models


def _span(start, end):
    return max(0, (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute))


def backfill_minutes_absent(apps, schema_editor):
    """Give every existing absence its session's scheduled length, resolved the
    same way get_effective_schedule does (per-employee override, else global)."""
    Absence = apps.get_model('attendance', 'Absence')
    GlobalSchedule = apps.get_model('organization', 'GlobalSchedule')
    EmployeeSchedule = apps.get_model('organization', 'EmployeeSchedule')

    g = GlobalSchedule.objects.filter(pk=1).first()
    defaults = {
        'am_in': g.am_in if g else time(8, 0), 'am_out': g.am_out if g else time(12, 0),
        'pm_in': g.pm_in if g else time(13, 0), 'pm_out': g.pm_out if g else time(17, 0),
    }
    overrides = {o.employee_id: o for o in EmployeeSchedule.objects.all()}

    def minutes(employee_id, session):
        o = overrides.get(employee_id)
        s = {f: (getattr(o, f, None) if o is not None else None) or defaults[f]
             for f in defaults}
        am, pm = _span(s['am_in'], s['am_out']), _span(s['pm_in'], s['pm_out'])
        return am if session == 'AM' else pm if session == 'PM' else am + pm

    for a in Absence.objects.all():
        a.minutes_absent = minutes(a.employee_id, a.session)
        a.save(update_fields=['minutes_absent'])


class Migration(migrations.Migration):

    dependencies = [
        ('attendance', '0005_alter_attendancelog_source'),
        ('organization', '0007_time_sync'),
    ]

    operations = [
        migrations.AddField(
            model_name='absence',
            name='minutes_absent',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(backfill_minutes_absent, migrations.RunPython.noop),
    ]
