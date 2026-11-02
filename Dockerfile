FROM python:3.12-alpine
# Shared sign-in library (the formlogin build context, see docker-compose.yaml).
COPY --from=formlogin formlogin /app/formlogin
ENV PYTHONPATH=/app
WORKDIR /app
CMD ["python", "-u", "/app/guard.py"]
