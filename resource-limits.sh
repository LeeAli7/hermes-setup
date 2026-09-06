#!/bin/bash
# Oracle Cloud Free Tier Resource Limits
FREE_MEM=$(free -m | awk "/^Mem:/{print \$7}")
DISK_USAGE=$(df -h / | awk "NR==2{print \$5}" | tr -d "%")
echo "Available memory: ${FREE_MEM}MB"
echo "Disk usage: ${DISK_USAGE}%"
if [ "$FREE_MEM" -lt 500 ]; then
    echo "WARNING: Low memory!"
    pkill -f "kilo_forwarder" 2>/dev/null
fi
if [ "$DISK_USAGE" -gt 85 ]; then
    echo "WARNING: High disk usage!"
    find /tmp -name "*.log" -mtime +7 -delete 2>/dev/null
fi
