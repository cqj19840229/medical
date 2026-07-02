$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ModelRoot = Join-Path $RepoRoot "models"
$HfHome = Join-Path $ModelRoot "huggingface"
$TransformersCache = Join-Path $HfHome "transformers"
$HubCache = Join-Path $HfHome "hub"

New-Item -ItemType Directory -Force -Path `
  $HfHome, `
  $TransformersCache, `
  $HubCache, `
  (Join-Path $ModelRoot "hub"), `
  (Join-Path $ModelRoot "adapters"), `
  (Join-Path $ModelRoot "embeddings") | Out-Null

$env:MODEL_ROOT = $ModelRoot
$env:HF_HOME = $HfHome
$env:TRANSFORMERS_CACHE = $TransformersCache
$env:HF_HUB_CACHE = $HubCache

Write-Host "Session environment variables are set for this PowerShell window:"
Write-Host "MODEL_ROOT=$env:MODEL_ROOT"
Write-Host "HF_HOME=$env:HF_HOME"
Write-Host "TRANSFORMERS_CACHE=$env:TRANSFORMERS_CACHE"
Write-Host "HF_HUB_CACHE=$env:HF_HUB_CACHE"
