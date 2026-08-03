#!/usr/bin/env bash
set -e
if ! command -v python3 &> /dev/null; then echo "Python 3 not found"; exit 1; fi
if [ ! -d ".venv" ]; then python3 -m venv .venv; fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
mkdir -p data logs
echo "Starting on http://0.0.0.0:8080"
cd backend
python3 main.py
