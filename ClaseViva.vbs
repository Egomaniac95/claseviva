' ClaseViva - doble clic para abrir la app (sin ventana de consola).
Option Explicit

Dim fso, sh, raiz, py, pyConsola, entrada, resultado, fallo
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
raiz = fso.GetParentFolderName(WScript.ScriptFullName)
py = raiz & "\.venv\Scripts\pythonw.exe"
pyConsola = raiz & "\.venv\Scripts\python.exe"
entrada = raiz & "\lanzador.py"

If Not fso.FileExists(py) Or Not fso.FileExists(pyConsola) Then
  Fallar "Falta el entorno .venv de ClaseViva." & vbCrLf & vbCrLf & _
    "Sigue la seccion Instalacion o reparacion de README.md para crearlo con Python 3.12 e instalar las dependencias."
End If
If Not fso.FileExists(entrada) Then
  Fallar "No se encuentra lanzador.py. Conserva ClaseViva.vbs dentro de la carpeta completa del programa."
End If

' Un venv puede conservar sus EXE aunque su Python base se haya movido o borrado.
On Error Resume Next
sh.CurrentDirectory = raiz
resultado = sh.Run(Comillas(pyConsola) & " --version", 0, True)
fallo = Err.Number
On Error GoTo 0
If fallo <> 0 Then
  Fallar "Windows no pudo ejecutar el Python de ClaseViva. Abre ClaseViva.bat para ver el diagnostico."
End If
If resultado <> 0 Then
  Fallar "El entorno .venv ya no puede ejecutar Python. Repara el entorno con Python 3.12 segun README.md."
End If

On Error Resume Next
sh.Run Comillas(py) & " " & Comillas(entrada), 0, False
fallo = Err.Number
On Error GoTo 0
If fallo <> 0 Then
  Fallar "Windows no pudo abrir ClaseViva. Abre ClaseViva.bat para ver el diagnostico."
End If

Function Comillas(ruta)
  Comillas = Chr(34) & ruta & Chr(34)
End Function

Sub Fallar(mensaje)
  MsgBox mensaje, 48, "ClaseViva"
  WScript.Quit 1
End Sub
