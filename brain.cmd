@echo off
rem Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
rem Opens the behaviour map page in the browser. The page runs in the background, without a window,
rem and stops by itself when its tab is closed. This window closes at once.
cd /d "%~dp0"
python ui.py --background %*
