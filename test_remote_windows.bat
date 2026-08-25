@echo off
set /p SMARTFETCH_URL=Paste your public SmartFetch URL (example https://abc.up.railway.app): 
python tests\remote_20.py %SMARTFETCH_URL%
pause
