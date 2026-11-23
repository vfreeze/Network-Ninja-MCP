' Runs its arguments as a command with no visible window and waits for it,
' so Task Scheduler sees the real exit code. Usage: wscript hidden.vbs prog.exe arg1 arg2 ...
cmd = ""
For Each a In WScript.Arguments
  cmd = cmd & " """ & a & """"
Next
WScript.Quit CreateObject("WScript.Shell").Run(Trim(cmd), 0, True)
