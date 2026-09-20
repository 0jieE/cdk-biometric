from django import template
from django.utils.safestring import mark_safe

register = template.Library()

_DAY_STATUS = {
    'PRESENT': ('bg-success', 'PRESENT'),
    'LATE': ('bg-warning text-dark', 'LATE'),
    'HALF_DAY': ('bg-info text-dark', 'HALF DAY'),
    'ABSENT': ('bg-danger', 'ABSENT'),
    'REST': ('bg-light text-dark', 'REST'),
}

_PUNCH = {
    'AM_IN': 'bg-success', 'PM_IN': 'bg-success', 'IN': 'bg-success',
    'AM_OUT': 'bg-secondary', 'PM_OUT': 'bg-secondary', 'OUT': 'bg-secondary',
    'OT_IN': 'bg-info text-dark', 'OT_OUT': 'bg-info text-dark',
}


@register.simple_tag
def day_status_badge(status):
    css, label = _DAY_STATUS.get(status, ('bg-light text-dark', status or '—'))
    return mark_safe(f'<span class="badge {css}">{label}</span>')


@register.simple_tag
def punch_badge(log_type):
    css = _PUNCH.get(log_type, 'bg-light text-dark')
    label = (log_type or '').replace('_', ' ')
    return mark_safe(f'<span class="badge {css}">{label}</span>')
