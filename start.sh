#!/bin/sh
set -e

if [ ! -f catboost_eth_model.cbm ]; then
  echo "No CatBoost model found. Training model..."
  python train_model.py || echo "WARNING: model training failed; server will start with 12h fallback."
fi

exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-10000}"
