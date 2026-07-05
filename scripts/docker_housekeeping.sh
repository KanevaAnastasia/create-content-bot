#!/usr/bin/env bash
set -euo pipefail

docker system prune -af
docker builder prune -af --reserved-space 2GB
docker system df
