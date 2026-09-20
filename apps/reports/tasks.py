"""Celery task for async report generation."""

from __future__ import annotations

import logging

from celery import shared_task
from django.core.files.base import ContentFile
from django.utils import timezone

from .generators import build_report
from .models import ReportJob

logger = logging.getLogger('apps.reports')


@shared_task(name='reports.generate_report')
def generate_report_task(job_id):
    """Build the file for an existing (PENDING) ReportJob and attach it.

    The view creates the ReportJob up front so the UI has an id to poll; this
    task fills in the file + status.
    """
    try:
        job = ReportJob.objects.get(pk=job_id)
    except ReportJob.DoesNotExist:
        logger.warning('ReportJob %s not found', job_id)
        return {'status': 'MISSING'}

    try:
        filename, content = build_report(job.report_type, job.fmt, job.params or {})
        job.file.save(filename, ContentFile(content), save=False)
        job.status = ReportJob.Status.DONE
        job.finished_at = timezone.now()
        job.error = ''
        job.save(update_fields=['file', 'status', 'finished_at', 'error'])
        logger.info('ReportJob %s DONE -> %s', job_id, filename)
        return {'status': 'DONE', 'file': job.file.name}
    except Exception as exc:
        logger.exception('ReportJob %s failed', job_id)
        job.status = ReportJob.Status.FAILED
        job.finished_at = timezone.now()
        job.error = str(exc)
        job.save(update_fields=['status', 'finished_at', 'error'])
        return {'status': 'FAILED', 'error': str(exc)}
