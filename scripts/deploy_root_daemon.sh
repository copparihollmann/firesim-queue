#!/usr/bin/env bash
# Deploy the firesim-queue daemon AS ROOT (Option A: it setuids each job to its
# submitter). Point FIRESIM_QUEUE_ROOT at your install/runtime dir.
#   Usage:  sudo FIRESIM_QUEUE_ROOT=/opt/firesim-queue bash deploy_root_daemon.sh
set -x
ROOT="${FIRESIM_QUEUE_ROOT:-/opt/firesim-queue}"
GRP="${FIRESIM_QUEUE_GROUP:-firesim}"   # shared group whose members may submit
pkill -f 'firesim_queue.py daemon' 2>/dev/null || true
for i in $(seq 1 40); do pgrep -f 'firesim_queue.py daemon' >/dev/null || break; sleep 2; done
pkill -9 -f 'firesim_queue.py daemon' 2>/dev/null || true
# launch as root, detached; the daemon drops privileges to each job's submitter
FIRESIM_QUEUE_ROOT="$ROOT" setsid bash -c \
  "umask 002; cd '$ROOT' && nohup /usr/bin/python3 bin/firesim_queue.py daemon >> daemon.log 2>&1 </dev/null &"
sleep 5
# clients submit as themselves -> queue.db must be group-writable
chgrp "$GRP" "$ROOT"/queue.db* 2>/dev/null || true
chmod g+w   "$ROOT"/queue.db* 2>/dev/null || true
echo "=== status ==="; "$ROOT/bin/firesim-queue" status | head -2
