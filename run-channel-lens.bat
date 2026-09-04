@echo off
REM Launch Channel Lens and open it in the default browser.
REM Keeps the window open on failure so the error is readable.
"%~dp0.venv\Scripts\python.exe" -m channel_lens %*
if errorlevel 1 pause
