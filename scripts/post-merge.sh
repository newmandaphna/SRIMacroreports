#!/bin/bash
# Post-merge setup script.
# Runs automatically after every task merge.
# Must be idempotent, non-interactive, and fast.
set -e

echo "--- post-merge: installing Python dependencies ---"
pip install -r requirements.txt --quiet

echo "--- post-merge: running database migrations ---"
alembic upgrade head

echo "--- post-merge: done ---"
