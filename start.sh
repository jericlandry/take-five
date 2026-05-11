#!/bin/bash

if [ -z "$PORT" ]; then
  lsof -ti:3000 | xargs kill -9 2>/dev/null || true
  (cd website && python3 -m http.server 3000) &
fi

uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}