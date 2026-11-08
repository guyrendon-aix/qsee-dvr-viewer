# Start the DVR dashboard. Pass -Lan to listen on all interfaces (no auth - trusted networks only).
param([switch]$Lan, [int]$Port = 8000)
$h = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
& "$PSScriptRoot\venv\Scripts\python.exe" -m uvicorn app.main:app --host $h --port $Port --app-dir $PSScriptRoot
