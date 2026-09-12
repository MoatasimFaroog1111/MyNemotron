FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# The Staff Control Plane is deliberately dependency-light. Copy only the
# runtime package instead of installing the full Nemotron training stack.
COPY src/nemotron /app/src/nemotron

RUN mkdir -p /data/staff /data/files \
    && addgroup --system staffcp \
    && adduser --system --ingroup staffcp staffcp \
    && chown -R staffcp:staffcp /data /app

USER staffcp

EXPOSE 8088

CMD ["python", "-m", "nemotron.staff.control_plane"]
