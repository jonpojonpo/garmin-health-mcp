# Runs the stdio MCP server, e.g. for registry checks (Glama) or containerised clients.
# Tools that read Garmin data need GARMIN_API_KEY; listing tools and prompts does not.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH" GARMIN_HEALTH_DATA_DIR=/data
VOLUME /data
ENTRYPOINT ["garmin-health-mcp"]
