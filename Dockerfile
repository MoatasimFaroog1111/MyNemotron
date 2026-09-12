FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# The Staff Control Plane is deliberately dependency-light. Copy only the
# runtime package instead of installing the full Nemotron training stack.
COPY src/nemotron /app/src/nemotron
COPY deploy/staff-entrypoint.py /app/staff-entrypoint.py

# Use a numeric unprivileged identity so the image does not depend on
# distribution-specific user-management utilities being installed.
RUN mkdir -p /data/staff/backups /data/files \
    && chown -R 65532:65532 /data /app

USER 65532:65532

EXPOSE 8088

# Railway volumes are mounted as root. If Railway starts this container with
# RAILWAY_RUN_UID=0, the entrypoint fixes only the controlled /data paths and
# immediately drops to the unprivileged application UID before exec'ing CMD.
ENTRYPOINT ["python", "/app/staff-entrypoint.py"]
CMD ["python", "-m", "nemotron.staff.control_plane"]
