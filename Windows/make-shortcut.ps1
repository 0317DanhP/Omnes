# Creates "Omnes.lnk" on the Desktop: the built .exe if there is one, otherwise the Python version.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe  = Join-Path $here "dist\Omnes\Omnes.exe"
$wsh  = New-Object -ComObject WScript.Shell
$lnk  = $wsh.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Omnes.lnk"))
if (Test-Path $exe) {
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = Split-Path $exe
} else {
    $lnk.TargetPath = Join-Path $here ".venv\Scripts\pythonw.exe"
    $lnk.Arguments = '"' + (Join-Path $here "run_datacleaner.py") + '"'
    $lnk.WorkingDirectory = $here
}
$lnk.IconLocation = Join-Path $here "assets\datacleaner.ico"
$lnk.Description = "Omnes"
$lnk.Save()
Write-Host "Shortcut created on the Desktop."
