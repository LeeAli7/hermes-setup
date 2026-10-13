#!/bin/bash
PYTHON=/home/ubuntu/.hermes/hermes-agent/venv/bin/python3
export FORWARDER_API_KEY="${FORWARDER_API_KEY:-2009muhammadali}"
nohup  $PYTHON /home/ubuntu/projects/hermes/forwarder.py 9000 > /tmp/forwarder.log 2>&1 &
nohup  /home/ubuntu/projects/hermes/kilo_forwarder.py 9001 > /tmp/kilo_forwarder.log 2>&1 &
echo Forwarders started
