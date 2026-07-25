' ClaseViva - doble clic para abrir la app (sin ventana de consola).
' Usa pythonw.exe del venv (firmado) -> Smart App Control no lo bloquea.
Set fso = CreateObject("Scripting.FileSystemObject")
raiz = fso.GetParentFolderName(WScript.ScriptFullName)
py = raiz & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
  MsgBox "Falta .venv. Crealo con Python 3.12, instala requirements.txt y despues ejecuta init.ps1 (ver README).", 48, "ClaseViva"
  WScript.Quit 1
End If
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = raiz
sh.Run """" & py & """ """ & raiz & "\lanzador.py""", 0, False
