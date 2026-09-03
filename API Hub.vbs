Option Explicit

Dim sh, fso, dir, cmd, i, Q

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
Q = Chr(34)

If IsRunning() Then
  MsgBox "API Hub is already running." & vbCrLf & vbCrLf & _
         "Use the tray icon (bottom-right, near the clock) to show the window." & vbCrLf & _
         "If no tray icon is visible, end the old process in Task Manager," & vbCrLf & _
         "then run this file again.", vbInformation, "API Hub"
  WScript.Quit
End If

cmd = Q & dir & "\.venv\Scripts\pythonw.exe" & Q & " desktop.py"

Dim attempt
For attempt = 1 To 3
  sh.Run cmd, 0, False
  For i = 1 To 30
    WScript.Sleep 500
    If IsRunning() Then Exit For
  Next
  If IsRunning() Then Exit For
  WScript.Sleep 2000   ' brief pause, then retry (port may be transiently busy)
Next

If Not IsRunning() Then
  MsgBox "API Hub failed to start." & vbCrLf & vbCrLf & _
         "Latest log:" & vbCrLf & LastLog() & vbCrLf & vbCrLf & _
         "If the log says the port is occupied, close every other API Hub" & vbCrLf & _
         "instance (Task Manager) and try again. Otherwise double-click" & vbCrLf & _
         "run.bat for the full console error.", vbExclamation, "API Hub"
End If

' Health probe: curl.exe directly, hidden (0), no cmd wrapper, no window flash
Function IsRunning()
  Dim code
  code = sh.Run(Q & "curl.exe" & Q & " -s -f -o nul -m 2 http://127.0.0.1:8787/health", 0, True)
  IsRunning = (code = 0)
End Function

' Read the last line of launch.log (startup failure reason)
Function LastLog()
  Dim f, txt, arr, n
  On Error Resume Next
  Set f = fso.OpenTextFile(dir & "\data\launch.log", 1, False)
  txt = f.ReadAll
  f.Close
  arr = Split(txt, vbLf)
  n = UBound(arr)
  If n >= 0 Then LastLog = Trim(arr(n)) Else LastLog = "(no log)"
  On Error GoTo 0
End Function
