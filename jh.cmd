@echo off
rem Launcher: jh <command> ...   (see README). Set JH_PYTHON to override the interpreter.
setlocal
set "HERE=%~dp0"
if "%JH_PYTHON%"=="" set "JH_PYTHON=C:\dev\prospeccao\.runtime\python\cpython-3.12.14-windows-x86_64-none\python.exe"
if not exist "%JH_PYTHON%" set "JH_PYTHON=py -3.12"
set "PYTHONPATH=%HERE%src"
%JH_PYTHON% -m job_hunter %*
