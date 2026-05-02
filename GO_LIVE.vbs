Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw VibeServer.py", 0, False
MsgBox "Cricket Trader is now running in the background." & vbCrLf & "Dashboard: http://localhost:8080", vbInformation, "Vibe Trader Live"
