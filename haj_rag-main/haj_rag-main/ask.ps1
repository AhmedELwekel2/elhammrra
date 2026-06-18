# Launch the interactive Hajj-book search.
# Usage:  .\ask.ps1            (top-5 hits)
#         .\ask.ps1 -k 3       (top-3 hits)
param([int]$k = 5)

chcp 65001 > $null                       # UTF-8 console so Arabic displays
$env:PYTHONIOENCODING = "utf-8"
Set-Location -Path $PSScriptRoot         # run from the repo root
python -m src.embed.embed --ask -k $k
