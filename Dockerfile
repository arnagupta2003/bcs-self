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

# Expose BombSquad default ports (TCP and UDP)
EXPOSE 43210/udp
EXPOSE 43210/tcp

# Run the modded server script
CMD ["./bombsquad_server"]