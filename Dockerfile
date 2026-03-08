FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1

# Copy project files
COPY pyproject.toml uv.lock ./

# Install dependencies in the system python environment
RUN uv pip install --system -r pyproject.toml

COPY . .

CMD ["python", "main.py"]
