#!/bin/bash
set -e

prometheus-node-exporter --web.listen-address=":9100" &

exec python /opt/app/app.py
