@echo off
setlocal

set "CODEPILOT_DEV_ROOT=D:\myCode\workflow"
if not defined CODEPILOT_PYTHON set "CODEPILOT_PYTHON=D:\FlyEnv-Data\env\python\python.exe"
if defined PYTHONPATH (
  set "PYTHONPATH=%CODEPILOT_DEV_ROOT%;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%CODEPILOT_DEV_ROOT%"
)

"%CODEPILOT_PYTHON%" -m codepilot %*
exit /b %ERRORLEVEL%
