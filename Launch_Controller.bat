@echo off
rem A.L.E.X. Controller launcher (2026-09-23).
rem
rem Craig had been launching the Controller from the IDE, and once from the
rem network share path; from a share every worktree and staging copy
rem inherits the UNC path and a staged copy took 113 seconds to come up
rem instead of 11. This always runs from the local project folder, with
rem pythonw so there is no console window. Make a desktop shortcut to it.
cd /d D:\project_ALEX\ALEX
start "" "C:\Users\Gaming Server\AppData\Local\Programs\Python\Python312\pythonw.exe" -X utf8 ALEX_Controller.py
