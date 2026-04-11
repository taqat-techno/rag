Set shell = CreateObject("Shell.Application")
Set folder = shell.BrowseForFolder(0, "Choose project folder", &H0051, "")
If Not folder Is Nothing Then
    WScript.Echo folder.Self.Path
End If
