@echo off
REM Launch the Python T24 runner from cmd. Environments resolve from the shared
REM store (run t24_env.py to manage them). Requires Python 3.8+ and paramiko.
python "%~dp0t24_run.py" %*
