' RAG Tools Smart Launcher
' - If service is running, opens admin panel in browser
' - If service is not running, starts it silently, waits, then opens browser
' - No terminal window shown to the user

Dim appDir, ragExe, shell, http, healthy

Set shell = CreateObject("WScript.Shell")
appDir = Replace(WScript.ScriptFullName, "\launch.vbs", "")
ragExe = appDir & "\rag.exe"

' Check if service is already healthy
healthy = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://127.0.0.1:21420/health", False
http.Send
If http.Status = 200 Then healthy = True
Set http = Nothing
On Error GoTo 0

If Not healthy Then
    ' Start the service silently
    shell.Run """" & ragExe & """ service start", 0, False

    ' Wait up to 30 seconds for service to become healthy
    Dim i
    For i = 1 To 30
        WScript.Sleep 1000
        On Error Resume Next
        Set http = CreateObject("MSXML2.XMLHTTP")
        http.Open "GET", "http://127.0.0.1:21420/health", False
        http.Send
        If http.Status = 200 Then
            healthy = True
            Set http = Nothing
            On Error GoTo 0
            Exit For
        End If
        Set http = Nothing
        On Error GoTo 0
    Next
End If

' Open admin panel in browser
If healthy Then
    shell.Run "http://127.0.0.1:21420", 1, False
End If
