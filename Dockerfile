# Use a lightweight Python image
FROM python:3.12-slim

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set the working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies into the system environment
RUN uv pip install --system -r pyproject.toml

# Copy the application code
COPY . .

# Expose the Gradio port
EXPOSE 7860

# Run the dashboard
CMD ["python", "dashboard.py"]
