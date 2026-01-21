#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

# Function to print status messages
log_info() {
    echo -e "\033[0;32m[INFO]\033[0m $1"
}

log_error() {
    echo -e "\033[0;31m[ERROR]\033[0m $1"
}

# Check if a filename was provided
if [ -z "$1" ]; then
    log_error "Please provide a service filename."
    echo "Usage: $0 <service_filename>"
    exit 1
fi

SERVICE_FILE="$1"
SERVICE_NAME=$(basename "$SERVICE_FILE")

# Check if the file exists in the current directory
if [ ! -f "$SERVICE_FILE" ]; then
    log_error "File '$SERVICE_FILE' not found in the current directory."
    exit 1
fi

log_info "Verifying service file syntax..."
# systemd-analyze verify can be noisy; we capture output and only show it on error
if ! output=$(systemd-analyze verify "./$SERVICE_FILE" 2>&1); then
    log_error "Verification failed:"
    echo "$output"
    exit 1
fi

log_info "Copying $SERVICE_NAME to /etc/systemd/system/..."
sudo cp "$SERVICE_FILE" "/etc/systemd/system/$SERVICE_NAME"

log_info "Reloading systemd daemon..."
sudo systemctl daemon-reload

log_info "Enabling $SERVICE_NAME to start on boot..."
sudo systemctl enable "$SERVICE_NAME"

log_info "Starting $SERVICE_NAME..."
sudo systemctl start "$SERVICE_NAME"

log_info "Deployment of $SERVICE_NAME complete and service is running."
