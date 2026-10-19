@echo off
setlocal

cd /d "%~dp0.."

python script\demo_basic_async.py --task trade --task-seed 0 --episode-count 20 --record-dir main_result
if errorlevel 1 exit /b %errorlevel%

python script\demo_direct_action.py --task trade --task-seed 0 --episode-count 20 --record-dir main_result
if errorlevel 1 exit /b %errorlevel%

python script\demo_parallelized.py --task trade --task-seed 0 --episode-count 20 --record-dir main_result
if errorlevel 1 exit /b %errorlevel%

python script\demo_agentic_cache.py --task trade --task-seed 0 --episode-count 20 --record-dir main_result
if errorlevel 1 exit /b %errorlevel%

python script\demo_autogen.py --task trade --task-seed 0 --episode-count 20 --record-dir main_result
if errorlevel 1 exit /b %errorlevel%

python script\demo_coap.py --task trade --task-seed 0 --episode-count 20 --record-dir main_result
if errorlevel 1 exit /b %errorlevel%

exit /b 0
