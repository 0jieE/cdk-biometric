"""Idempotent seed data for Phase 8 development.

Creates the GlobalSchedule, a full-time/part-time employee mix (including a
Saturday worker), an admin superuser, a biometric device, the two fixed-date
national holidays, and the periodic sync task.

Deliberately seeds NOTHING that changes how real punches are counted — no
invented holidays, no overtime authorizations. This runs on every container
start (SEED_ON_START=true), so anything date-relative it created piled up: a
fake "Foundation Day" holiday for every month it ran in, and an example OT
authorization for every day it ran on. Holidays/OT belong to the admin.
"""

from datetime import date, time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.devices.models import BiometricDevice
from apps.organization.models import (
    Department,
    Employee,
    EmployeeSchedule,
    GlobalSchedule,
    Holiday,
)

User = get_user_model()

EMPLOYEE_PASSWORD = 'employee12345'
MON_FRI = [0, 1, 2, 3, 4]
MON_SAT = [0, 1, 2, 3, 4, 5]

DEPARTMENTS = [
    {'name': 'Human Resources', 'code': 'HR'},
    {'name': 'Information Technology', 'code': 'IT'},
    {'name': 'College Faculty', 'code': 'FAC'},
]

# bio % 4 == 0 -> chronically late; bio % 5 == 0 (full-time) -> OT-eligible in the
# mock; bio % 5 == 1 (full-time) -> unauthorized late-leaver (proves no auto-OT).
EMPLOYEES = [
    {'no': 'EMP-1001', 'first': 'Maria',  'last': 'Santos',     'dept': 'HR',  'pos': 'HR Officer',       'bio': '1001', 'fulltime': True},
    {'no': 'EMP-1002', 'first': 'Jose',   'last': 'Reyes',      'dept': 'HR',  'pos': 'HR Assistant',     'bio': '1002', 'fulltime': True},
    {'no': 'EMP-1003', 'first': 'Ana',    'last': 'Cruz',       'dept': 'IT',  'pos': 'System Admin',     'bio': '1003', 'fulltime': True},
    {'no': 'EMP-1004', 'first': 'Pedro',  'last': 'Bautista',   'dept': 'IT',  'pos': 'Developer',        'bio': '1004', 'fulltime': True},
    {'no': 'EMP-1005', 'first': 'Liza',   'last': 'Garcia',     'dept': 'FAC', 'pos': 'Instructor',       'bio': '1005', 'fulltime': True, 'saturday': True},
    {'no': 'EMP-1006', 'first': 'Mark',   'last': 'Torres',     'dept': 'FAC', 'pos': 'Instructor',       'bio': '1006', 'fulltime': True},
    {'no': 'EMP-1007', 'first': 'Grace',  'last': 'Villanueva', 'dept': 'FAC', 'pos': 'Part-time Lecturer', 'bio': '1007', 'fulltime': False},
    {'no': 'EMP-1008', 'first': 'Ramon',  'last': 'Aquino',     'dept': 'IT',  'pos': 'Network Engineer', 'bio': '1008', 'fulltime': True},
    {'no': 'EMP-1009', 'first': 'Nina',   'last': 'Flores',     'dept': 'HR',  'pos': 'Part-time Clerk',  'bio': '1009', 'fulltime': False},
    {'no': 'EMP-1010', 'first': 'Ben',    'last': 'Ramos',      'dept': 'IT',  'pos': 'Support Staff',    'bio': '1010', 'fulltime': True},
]


class Command(BaseCommand):
    help = 'Seed the database with Phase 8 development data (idempotent).'

    @transaction.atomic
    def handle(self, *args, **options):
        created = {'departments': 0, 'employees': 0, 'users': 0,
                   'overrides': 0, 'holidays': 0, 'devices': 0}

        # Global schedule (institution default).
        schedule = GlobalSchedule.load()
        schedule.am_in, schedule.am_out = time(8, 0), time(12, 0)
        schedule.pm_in, schedule.pm_out = time(13, 0), time(17, 0)
        schedule.grace_period_minutes = 5
        schedule.workdays = MON_FRI
        schedule.save()

        # Departments
        dept_by_code = {}
        for d in DEPARTMENTS:
            obj, was_created = Department.objects.get_or_create(
                code=d['code'], defaults={'name': d['name'], 'is_active': True})
            dept_by_code[d['code']] = obj
            created['departments'] += int(was_created)

        # Employees + users (+ Saturday override where flagged)
        for e in EMPLOYEES:
            emp, emp_created = Employee.objects.get_or_create(
                employee_no=e['no'],
                defaults={
                    'first_name': e['first'], 'last_name': e['last'],
                    'department': dept_by_code[e['dept']], 'position': e['pos'],
                    'date_hired': date(2023, 1, 15), 'biometric_id': e['bio'],
                    'is_active': True, 'is_fulltime': e['fulltime'],
                },
            )
            # Keep is_fulltime in sync on re-seed.
            if emp.is_fulltime != e['fulltime']:
                emp.is_fulltime = e['fulltime']
                emp.save(update_fields=['is_fulltime'])
            created['employees'] += int(emp_created)

            if e.get('saturday'):
                _, ov_created = EmployeeSchedule.objects.get_or_create(
                    employee=emp, defaults={'workdays': MON_SAT})
                created['overrides'] += int(ov_created)

            username = e['no'].lower()
            # Look the account up by its EMPLOYEE first: employees can rename their
            # own login from the mobile app, and this runs on every container start.
            # Keyed on the username alone it would not find a renamed account, try to
            # create a second user for the same employee, and hit the one-to-one
            # constraint — crashing the entrypoint and stopping the app from booting.
            user = User.objects.filter(employee=emp).first()
            user_created = False
            if user is None:
                user, user_created = User.objects.get_or_create(
                    username=username,
                    defaults={
                        'email': f'{username}@ckc.edu.ph', 'role': User.Roles.EMPLOYEE,
                        'employee': emp, 'first_name': e['first'], 'last_name': e['last'],
                    },
                )
            if user_created:
                user.set_password(EMPLOYEE_PASSWORD)
                user.save()
            created['users'] += int(user_created)

        # Admin superuser
        admin_password = getattr(settings, 'ADMIN_PASSWORD', 'admin12345')
        admin, admin_created = User.objects.get_or_create(
            username='admin',
            defaults={'email': 'admin@ckc.edu.ph', 'role': User.Roles.ADMIN,
                      'is_staff': True, 'is_superuser': True})
        if admin_created:
            admin.set_password(admin_password)
            admin.save()

        # Biometric device
        _, dev_created = BiometricDevice.objects.get_or_create(
            name='Main Entrance ZKTeco',
            defaults={'ip_address': settings.ZK_DEVICE_IP, 'port': settings.ZK_DEVICE_PORT,
                      'location': 'Main Building Lobby', 'serial_no': 'ZK-SIM-0001',
                      'is_active': True})
        created['devices'] += int(dev_created)

        # Fixed-date national holidays only. (The rest come from the portal's
        # holiday import; never invent a holiday relative to "today".)
        today = timezone.localdate()
        holidays = [
            {'date': date(today.year, 1, 1),  'name': "New Year's Day",   'type': Holiday.Types.REGULAR, 'recurring': True},
            {'date': date(today.year, 6, 12), 'name': 'Independence Day', 'type': Holiday.Types.REGULAR, 'recurring': True},
        ]
        for h in holidays:
            _, h_created = Holiday.objects.get_or_create(
                date=h['date'],
                defaults={'name': h['name'], 'type': h['type'], 'is_recurring': h['recurring']})
            created['holidays'] += int(h_created)

        self._seed_periodic_sync()

        # Summary
        fulltime = [e['no'] for e in EMPLOYEES if e['fulltime']]
        parttime = [e['no'] for e in EMPLOYEES if not e['fulltime']]
        saturday = [e['no'] for e in EMPLOYEES if e.get('saturday')]

        self.stdout.write(self.style.SUCCESS('Seed complete.'))
        for key, count in created.items():
            self.stdout.write(f'  {key:<12}: +{count} new (idempotent)')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Employee mix:'))
        self.stdout.write(f'  Full-time ({len(fulltime)}): {", ".join(fulltime)}')
        self.stdout.write(f'  Part-time ({len(parttime)}): {", ".join(parttime)}')
        self.stdout.write(f'  Saturday worker: {", ".join(saturday)} (EmployeeSchedule override)')
        self.stdout.write(f'  OT-eligible in mock: EMP-1005, EMP-1010 (bio % 5 == 0)')
        self.stdout.write(f'  Unauthorized late-leaver: EMP-1001, EMP-1006 (no OT expected)')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Admin login (web portal / Django admin):'))
        self.stdout.write(f'  username: admin')
        self.stdout.write(f'  password: {admin_password}')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Sample employee login (mobile API):'))
        self.stdout.write(f'  full-time: emp-1001 / {EMPLOYEE_PASSWORD}')
        self.stdout.write(f'  part-time: emp-1007 / {EMPLOYEE_PASSWORD}')

    def _seed_periodic_sync(self):
        from django_celery_beat.models import IntervalSchedule, PeriodicTask

        minutes = getattr(settings, 'SYNC_INTERVAL_MINUTES', 5)
        sync_schedule, _ = IntervalSchedule.objects.get_or_create(
            every=minutes, period=IntervalSchedule.MINUTES)
        PeriodicTask.objects.update_or_create(
            name='Sync attendance from device',
            defaults={'interval': sync_schedule, 'task': 'attendance.sync_attendance',
                      'enabled': True})

        # Outage-recovery sweeper for stuck notifications (every 2 minutes).
        sweep_schedule, _ = IntervalSchedule.objects.get_or_create(
            every=2, period=IntervalSchedule.MINUTES)
        PeriodicTask.objects.update_or_create(
            name='Retry pending notifications',
            defaults={'interval': sweep_schedule,
                      'task': 'notifications.retry_pending', 'enabled': True})

        self.stdout.write(self.style.SUCCESS(
            f'Periodic sync every {minutes} min; notification sweeper every 2 min.'))
