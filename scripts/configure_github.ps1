param(
  [Parameter(Mandatory = $true)][string]$Owner,
  [Parameter(Mandatory = $true)][string]$Repo,
  [string]$DefaultBranch = "main-clean",
  [bool]$EnableAutoDelete = $true,
  [bool]$ProtectBranch = $true,
  [bool]$DeleteOldMain = $false
)

# Ensure gh is available
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
  $defaultGh = 'C:\\Program Files\\GitHub CLI\\gh.exe'
  if (Test-Path $defaultGh) { $env:Path = "C:\\Program Files\\GitHub CLI;" + $env:Path } else {
    Write-Error "GitHub CLI (gh) not found. Install it first."; exit 1
  }
}

# Check auth status
try {
  $auth = (gh auth status 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0) { throw 'Not authenticated' }
  Write-Host "gh auth status: OK"
} catch {
  Write-Error "GitHub CLI not authenticated. Run: gh auth login (or set GH_TOKEN)"; exit 1
}

Write-Host "Target repo: $Owner/$Repo"

function Set-DefaultBranch {
  param([string]$Owner,[string]$Repo,[string]$Branch)
  Write-Host "Setting default branch -> $Branch"
  gh api -X PATCH repos/$Owner/$Repo -f default_branch=$Branch | Out-Null
}

function Enable-AutoDeleteHeadBranches {
  param([string]$Owner,[string]$Repo)
  Write-Host "Enabling auto-delete of head branches"
  gh api -X PATCH repos/$Owner/$Repo -f delete_branch_on_merge=true | Out-Null
}

function Protect-Branch {
  param([string]$Owner,[string]$Repo,[string]$Branch)
  Write-Host "Applying protection rules on $Branch"
  $body = @{
    required_status_checks = $null
    enforce_admins = $true
    required_pull_request_reviews = @{ 
      required_approving_review_count = 1
      require_last_push_approval = $true
      dismiss_stale_reviews = $true
      require_code_owner_reviews = $false
    }
    restrictions = $null
    required_linear_history = $true
    allow_force_pushes = $false
    allow_deletions = $false
    required_conversation_resolution = $true
    block_creations = $false
  } | ConvertTo-Json -Depth 5

  gh api `
    -X PUT `
    -H "Accept: application/vnd.github+json" `
    -H "Content-Type: application/json" `
    repos/$Owner/$Repo/branches/$Branch/protection `
    -d $body | Out-Null
}

function Remove-BranchMainIfRequested {
  param([string]$Owner,[string]$Repo)
  if ($DeleteOldMain) {
    try {
      Write-Host "Deleting old 'main' branch (remote)"
      gh api -X DELETE repos/$Owner/$Repo/git/refs/heads/main | Out-Null
    } catch {
      Write-Warning "Could not delete 'main' branch: $($_.Exception.Message)"
    }
  }
}

# Execute steps
Set-DefaultBranch -Owner $Owner -Repo $Repo -Branch $DefaultBranch
if ($EnableAutoDelete) { Enable-AutoDeleteHeadBranches -Owner $Owner -Repo $Repo }
if ($ProtectBranch) { Protect-Branch -Owner $Owner -Repo $Repo -Branch $DefaultBranch }
Remove-BranchMainIfRequested -Owner $Owner -Repo $Repo

Write-Host "Done."

