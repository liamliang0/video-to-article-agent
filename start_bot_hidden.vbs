Option Explicit

Dim fso, shell, baseDir, pythonExe, botScript, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

If WScript.Arguments.Named.Exists("check") Then WScript.Quit 0

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonExe = baseDir & "\.venv\Scripts\pythonw.exe"
botScript = baseDir & "\bot.py"

If Not fso.FileExists(pythonExe) Then
    MsgBox "Python environment not found. Please create .venv first.", 16, "Bilibili Bot"
    WScript.Quit 1
End If

shell.CurrentDirectory = baseDir
command = Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & botScript & Chr(34)
shell.Run command, 0, False
MsgBox "Bot started in the background. You may close all windows. Logs: bot.log", 64, "Bilibili Bot"
