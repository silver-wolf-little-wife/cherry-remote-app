' Cherry Remote App - hidden launcher for auto-start
Dim fso : Set fso = CreateObject("Scripting.FileSystemObject")
Dim dir : dir = fso.GetParentFolderName(WScript.ScriptFullName)
Dim shell : Set shell = CreateObject("Wscript.Shell")
shell.Run chr(34) & dir & "\start.bat" & chr(34), 0, False
