' run-seneschald-update-hidden.vbs — launch seneschald-control.ps1 -Action Update with NO visible window.
'
' Task Scheduler runs pwsh.exe as a console app, so a console window flashes on the desktop every ~10
' min and steals focus (disrupts typing / full-screen games). Point the seneschald-update task at THIS file
' via wscript.exe instead: wscript is windowless, and the Run(..., 0, ...) launches pwsh with a hidden
' window — so nothing ever appears. The PowerShell still runs in the same interactive session, so git
' auth / credential manager are unchanged.
'
' Task action:  Program/script: wscript.exe
'               Arguments:      "<repo>\seneschal\scripts\run-seneschald-update-hidden.vbs"
' (See PATH_A_CUTOVER.md step 5.) Resolves its own folder, so it's host-independent.
Option Explicit
Dim fso, here, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "pwsh.exe -NoProfile -ExecutionPolicy Bypass -File """ & here & "\seneschald-control.ps1"" -Action Update"
CreateObject("WScript.Shell").Run cmd, 0, False   ' 0 = hidden window, False = don't wait
