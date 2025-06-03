#!/bin/bash
source ~/venv/bin/activate
export MCADVCHAT_ENV=dev
python C2-mc-ws.py "$@"
