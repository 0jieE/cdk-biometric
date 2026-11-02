from django.db import migrations


def drop_parttime_absences(apps, schema_editor):
    """Part-timers are now judged per week on hours rendered, never marked
    absent per day; clear the daily absences recorded under the old rule so
    the Absences page and lost-time totals stop counting them."""
    Absence = apps.get_model('attendance', 'Absence')
    Absence.objects.filter(employee__is_fulltime=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('attendance', '0007_processing_cursor'),
        ('organization', '0008_parttime_weekly_hours'),
    ]

    operations = [
        migrations.RunPython(drop_parttime_absences, migrations.RunPython.noop),
    ]
