@echo off
REM Dvoklik: slozi fajlove, provjeri/preuzme/azurira sve i napravi exe.
REM Za samo pripremu (bez exe-a):  setup_and_build.bat --no-build
cd /d "%~dp0"
python setup_and_build.py %*
pause
