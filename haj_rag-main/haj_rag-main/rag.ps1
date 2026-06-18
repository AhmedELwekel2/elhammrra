# Launch the interactive Hajj-book Q&A (retrieve + GPT-5.2 grounded answer).
# Usage:  .\rag.ps1            (retrieve 6 chunks per question)
#         .\rag.ps1 -k 8
param([int]$k = 6)

chcp 65001 > $null                       # UTF-8 console so Arabic displays
$env:PYTHONIOENCODING = "utf-8"
Set-Location -Path $PSScriptRoot         # run from the repo root
python -m src.generate.answer --ask -k $k
