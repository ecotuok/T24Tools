@echo off
REM Launch the Python T24 runner from cmd. The CSV is looked for in the CURRENT
REM directory. Requires Python 3.8+ and paramiko (pip install paramiko).
python "%~dp0t24_run.py" %*
