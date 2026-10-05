FROM python:3.13-slim

# Install runtime utilities if needed
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Fix for /etc/machine-id missing in Docker containers
RUN echo "b08df8027a03428886367963b27b3d36" > /etc/machine-id && \
    chmod 444 /etc/machine-id

# Set working directory inside the container
WORKDIR /app

# Copy all repository contents into the container
COPY . /app

# Ensure execution permissions for the server binaries
RUN chmod +x bombsquad_server dist/bombsquad_headless

# Expose BombSquad gameplay and the private admin console.
EXPOSE 43210/udp
EXPOSE 8080/tcp
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENV ADMIN_UI_HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1

# The admin panel supervises the normal BombSquad server manager.
CMD ["python3", "admin_panel.py"]