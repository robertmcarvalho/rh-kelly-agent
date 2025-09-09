<#!
.SYNOPSIS
  Checklist para setup de MCPs e ferramentas locais.

.USAGE
  pwsh -File scripts/setup_mcp.ps1
#>

param()

Write-Host "== Verificando gcloud..." -ForegroundColor Cyan
gcloud --version | Out-Host

Write-Host "== Config atual do gcloud..." -ForegroundColor Cyan
gcloud config list | Out-Host

Write-Host "== Verificando Docker..." -ForegroundColor Cyan
docker version | Out-Host

Write-Host "== Verificando gh..." -ForegroundColor Cyan
gh --version | Out-Host

Write-Host "== Serviços Cloud Run (us-central1)..." -ForegroundColor Cyan
gcloud run services list --region us-central1 --platform managed | Out-Host

Write-Host "== Buckets GCS..." -ForegroundColor Cyan
gcloud storage buckets list | Out-Host

Write-Host "== Dicas próximas:"
Write-Host "- Configure MCPs na IDE: GCP, GCS, Postgres, Redis, HTTP/GraphQL, GitHub, Docker" -ForegroundColor Yellow
Write-Host "- Use a coleção http/panel-api.http para testar a API" -ForegroundColor Yellow
Write-Host "- Defina DATABASE_URL para Postgres quando disponível" -ForegroundColor Yellow

