# Public showcase: synthetic corpus, stdlib retrieval, no model credentials.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8080
WORKDIR /srv/app
RUN chown 10001:10001 /srv/app
COPY --chown=10001:10001 app/underwriter_server.py app/permission_rag.py app/llm.py app/ui.html app/presets.json ./
USER 10001:10001
EXPOSE 8080
CMD ["python", "underwriter_server.py"]
