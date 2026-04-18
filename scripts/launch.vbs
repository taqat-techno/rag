' RAG Tools Smart Launcher (with self-healing restart-once fallback)
'
' Behavior:
'   1. If service is already healthy, open admin panel in browser and exit.
'   2. If not, start the service silently and wait up to 30 seconds for
'      /health to become 200.
'   3. If the health check still fails, try one restart (stop + start) and
'      wait another 30 seconds. This catches the "service crashed shortly
'      after startup" pattern reported by field users.
'   4. If still unhealthy, log a note to the data directory and exit without
'      opening the browser (so the user isn't shown a broken page).
'
' Never shows a terminal window to the user.

Option Explicit

Dim appDir, ragExe, shell, http, healthy, attempts, maxAttempts
Dim logMsg, fso, logPath, appData, logFile

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = Replace(WScript.ScriptFullName, "\launch.vbs", "")
ragExe = appDir & "\rag.exe"
appData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
logPath = appData & "\RAGTools\logs\launcher.log"

' --- Helper: probe health, return True/False ---
Function IsHealthy()
    Dim probe
    IsHealthy = False
    On Error Resume Next
    Set probe = CreateObject("MSXML2.XMLHTTP")
    probe.Open "GET", "http://127.0.0.1:21420/health", False
    probe.Send
    If Err.Number = 0 And probe.Status = 200 Then IsHealthy = True
    Set probe = Nothing
    On Error GoTo 0
End Function

' --- Helper: append one line to the launcher log ---
Sub LogLine(msg)
    On Error Resume Next
    Dim ts, dir
    ts = Now
    dir = fso.GetParentFolderName(logPath)
    If Not fso.FolderExists(dir) Then fso.CreateFolder(dir)
    Set logFile = fso.OpenTextFile(logPath, 8, True)  ' ForAppending=8, Create=True
    logFile.WriteLine ts & " " & msg
    logFile.Close
    On Error GoTo 0
End Sub

' --- Helper: wait up to N seconds for health ---
Function WaitForHealthy(secs)
    Dim i
    WaitForHealthy = False
    For i = 1 To secs
        WScript.Sleep 1000
        If IsHealthy() Then
            WaitForHealthy = True
            Exit Function
        End If
    Next
End Function

' --- Main flow ---
healthy = IsHealthy()

If Not healthy Then
    LogLine "Service not running. Starting…"
    shell.Run """" & ragExe & """ service start", 0, False

    If WaitForHealthy(30) Then
        healthy = True
        LogLine "Service became healthy on first start."
    End If
End If

' First restart-once fallback: if service went up and then died, or never came up
If Not healthy Then
    LogLine "First start did not produce a healthy service. Restarting once…"
    ' Stop any half-started instance first (best-effort, ignore errors)
    On Error Resume Next
    shell.Run """" & ragExe & """ service stop", 0, True
    On Error GoTo 0
    WScript.Sleep 2000

    shell.Run """" & ragExe & """ service start", 0, False

    If WaitForHealthy(30) Then
        healthy = True
        LogLine "Service became healthy after restart."
    Else
        LogLine "Service still not healthy after restart. Giving up without opening browser."
    End If
End If

' Open admin panel in browser only if we really got a healthy service
If healthy Then
    shell.Run "http://127.0.0.1:21420", 1, False
End If
