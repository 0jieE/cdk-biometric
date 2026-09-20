# There is no official "Django" image — Django is a Python library, so we start
# from the official Python image and install Django (and the rest) from
# requirements.txt on top of it.
FROM python:3.12-slim

# Faster, cleaner Python in containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System deps needed to build/run mysqlclient, plus a MySQL client for waits.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        default-libmysqlclient-dev \
        pkg-config \
        default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer is cached until requirements change.
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the rest of the backend (bind-mounted over in dev; baked in for prod).
COPY . /app

# Entrypoint handles wait-for-db, migrate, seed, collectstatic, then exec CMD.
COPY entrypoint.sh /entrypoint.sh
# Strip any CRLF (Windows) line endings so the script runs under Linux, then arm it.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Prod-shaped default; the dev override replaces this with runserver.
CMD ["gunicorn", "core.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
