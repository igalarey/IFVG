' =====================================================================
'  run_app.vbs  —  launch the IFVG dashboard with NO visible cmd window
'  Double-click THIS (instead of run_app.bat) to run the bot hidden.
'  It starts run_app.bat (the auto-restart loop) with a hidden window,
'  so nothing shows on screen; open the panel yourself at
'  http://127.0.0.1:8765 whenever you want to check it.
'
'  To STOP it: stop it from Task Scheduler, or end the python.exe /
'  cmd.exe processes in Task Manager.
' =====================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
' window style 0 = hidden, False = do not wait
sh.Run "cmd /c """ & here & "\run_app.bat""", 0, False
