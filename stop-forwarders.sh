#!/bin/bash
pkill -f 'forwarder.py 9000' 2>/dev/null
pkill -f 'kilo_forwarder.py 9001' 2>/dev/null
echo Forwarders stopped
