#!/usr/bin/env python3
"""Toggle Tapo camera date/time OSD. Usage: osd.py <ip> <0|1>  (TAPO_PASS in env)."""
import sys, os
from pytapo import Tapo
ip = sys.argv[1]
enable = len(sys.argv) > 2 and sys.argv[2] == "1"
t = Tapo(ip, "admin", os.environ["TAPO_PASS"])
t.setOsd(label="", dateEnabled=enable, labelEnabled=False)
print("OSD date " + ("enabled" if enable else "disabled") + " on " + ip)
