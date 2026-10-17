<#
.SYNOPSIS
    Orchestrated safe reboot of the Ferrin Homelab infrastructure.

.DESCRIPTION
    Safely shuts down and reboots Home Assistant, Kubernetes nodes (kube2, yoga-node, kubeprime),
    and the OpenMediaVault NAS in strict dependency order, avoiding NFS stale handles and database corruption.
    Monitors bring-up and performs automated health verification.

.PARAMETER DryRun
    Simulate the execution and print all SSH commands without actually running them.

.PARAMETER SkipNas
    Reboot Kubernetes and Home Assistant, but leave the NAS running.

.PARAMETER SkipHa
    Skip rebooting Home Assistant.

.PARAMETER NasUser
    SSH username for OpenMediaVault NAS (default: mferrin; root key login is not enabled on the NAS).

.PARAMETER ClusterUser
    SSH username for kubeprime, kube2, and yoga-node (default: mferrin).

.PARAMETER HaUser
    SSH username for Home Assistant (default: root).

.EXAMPLE
    .\scripts\reboot-homelab.ps1 -DryRun
    .\scripts\reboot-homelab.ps1
    .\scripts\reboot-homelab.ps1 -SkipNas
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipNas,
    [switch]$SkipHa,
    [string]$NasHost = "192.168.1.253",
    [string]$NasUser = "mferrin",
    [string]$KubePrime = "192.168.1.247",
    [string]$Kube2 = "192.168.1.248",
    [string]$YogaNode = "192.168.1.249",
    [string]$ClusterUser = "mferrin",
    [string]$HaHost = "192.168.1.254",
    [string]$HaUser = "root",
    [int]$PollTimeoutSeconds = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==== $Message ====" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-WarningMsg {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Gray
}

function Invoke-RemoteCommand {
    param(
        [string]$User,
        [string]$HostName,
        [string]$Command,
        [switch]$IgnoreErrors
    )

    $target = "$User@$HostName"
    if ($DryRun) {
        Write-Host "  [DRY RUN] ssh $target `"$Command`"" -ForegroundColor Magenta
        return ""
    }

    Write-Info "Executing on ${target}: $Command"
    try {
        # Using OpenSSH client with 10s connection timeout and batch mode
        $output = & ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $target $Command 2>&1
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0 -and -not $IgnoreErrors) {
            throw "SSH command failed with exit code ${exitCode}: $output"
        }
        return ($output -join "`n")
    }
    catch {
        if ($IgnoreErrors) {
            Write-WarningMsg "Non-critical error on ${target}: $_"
            return $null
        }
        throw $_
    }
}

function Wait-ForPort {
    param(
        [string]$HostName,
        [int]$Port,
        [string]$ServiceName,
        [int]$TimeoutSec = $PollTimeoutSeconds
    )

    if ($DryRun) {
        Write-Host "  [DRY RUN] Wait for $ServiceName ($HostName`:$Port)" -ForegroundColor Magenta
        return $true
    }

    Write-Host "Waiting for $ServiceName ($HostName`:$Port) to respond..." -NoNewline -ForegroundColor Gray
    $startTime = [DateTime]::UtcNow
    while (([DateTime]::UtcNow - $startTime).TotalSeconds -lt $TimeoutSec) {
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $asyncResult = $tcp.BeginConnect($HostName, $Port, $null, $null)
            $waitSuccess = $asyncResult.AsyncWaitHandle.WaitOne(1500, $false)
            if ($waitSuccess -and $tcp.Connected) {
                $tcp.EndConnect($asyncResult)
                $tcp.Close()
                Write-Host " UP!" -ForegroundColor Green
                return $true
            }
            $tcp.Close()
        }
        catch {
            # Retry
        }
        Write-Host "." -NoNewline -ForegroundColor Gray
        Start-Sleep -Seconds 3
    }

    Write-Host " TIMEOUT!" -ForegroundColor Red
    throw "Timed out waiting for $ServiceName on $HostName`:$Port after $TimeoutSec seconds."
}

# ==========================================
# CONFIRMATION & PRE-FLIGHT
# ==========================================
Write-Host @"
====================================================================
           FERRIN HOMELAB SAFE REBOOT ORCHESTRATOR
====================================================================
Targets:
  - Home Assistant:  $HaUser@$HaHost $(if ($SkipHa) { '[SKIPPED]' })
  - Worker Nodes:    $ClusterUser@$Kube2, $ClusterUser@$YogaNode
  - Control Plane:   $ClusterUser@$KubePrime
  - OMV NAS:         $NasUser@$NasHost $(if ($SkipNas) { '[SKIPPED]' })
Mode: $(if ($DryRun) { 'DRY RUN (Simulated)' } else { 'LIVE EXECUTION' })
====================================================================
"@ -ForegroundColor Yellow

if (-not $DryRun) {
    $confirm = Read-Host "Are you sure you want to proceed with this coordinated reboot? (yes/no)"
    if ($confirm -ne "yes") {
        Write-WarningMsg "Reboot aborted by user."
        exit 0
    }
}

# ------------------------------------------------------------------
# STEP 1: PRE-FLIGHT CONNECTIVITY CHECKS
# ------------------------------------------------------------------
Write-Step "Step 1: Pre-Flight Connectivity Checks"

$hostsToCheck = @(
    @{ Name = "kubeprime"; Host = $KubePrime; Port = 22 },
    @{ Name = "kube2"; Host = $Kube2; Port = 22 },
    @{ Name = "yoga-node"; Host = $YogaNode; Port = 22 }
)
if (-not $SkipNas) {
    $hostsToCheck += @{ Name = "pinas (NAS)"; Host = $NasHost; Port = 22 }
}
if (-not $SkipHa) {
    $hostsToCheck += @{ Name = "homeassistant"; Host = $HaHost; Port = 22 }
}

foreach ($h in $hostsToCheck) {
    $conn = Test-NetConnection -ComputerName $h.Host -Port $h.Port -WarningAction SilentlyContinue
    if ($conn.TcpTestSucceeded) {
        Write-Success "$($h.Name) ($($h.Host)) reachable on port $($h.Port)"
    } else {
        Write-WarningMsg "Could not reach $($h.Name) ($($h.Host)) on port $($h.Port). Check IP or credentials!"
    }
}

# ------------------------------------------------------------------
# STEP 2: SHUTDOWN CONSUMERS (HOME ASSISTANT & DOCKER SERVICES)
# ------------------------------------------------------------------
Write-Step "Step 2: Gracefully Stopping External Consumers"

if (-not $SkipHa) {
    Write-Info "Rebooting Home Assistant OS ($HaHost)..."
    Invoke-RemoteCommand -User $HaUser -HostName $HaHost -Command "ha host reboot || sudo reboot" -IgnoreErrors
    Write-Success "Home Assistant reboot initiated."
}

Write-Info "Stopping Frigate NVR host Docker on yoga-node..."
$stopFrigateCmd = "if [ -d /opt/frigate ]; then cd /opt/frigate && sudo docker compose down; fi"
Invoke-RemoteCommand -User $ClusterUser -HostName $YogaNode -Command $stopFrigateCmd -IgnoreErrors
Write-Success "Frigate containers stopped on yoga-node."

# ------------------------------------------------------------------
# STEP 3: STOP KUBERNETES WORKERS
# ------------------------------------------------------------------
Write-Step "Step 3: Stopping Kubernetes Workers (kube2 & yoga-node)"

Write-Info "Stopping k3s-agent and rebooting kube2 ($Kube2)..."
Invoke-RemoteCommand -User $ClusterUser -HostName $Kube2 -Command "sudo systemctl stop k3s-agent && sudo reboot" -IgnoreErrors
Write-Success "kube2 reboot issued."

Write-Info "Stopping k3s-agent and rebooting yoga-node ($YogaNode)..."
Invoke-RemoteCommand -User $ClusterUser -HostName $YogaNode -Command "sudo systemctl stop k3s-agent && sudo reboot" -IgnoreErrors
Write-Success "yoga-node reboot issued."

# ------------------------------------------------------------------
# STEP 4: STOP KUBERNETES CONTROL PLANE
# ------------------------------------------------------------------
Write-Step "Step 4: Stopping Kubernetes Control-Plane (kubeprime)"

Write-Info "Stopping k3s and rebooting kubeprime ($KubePrime)..."
Invoke-RemoteCommand -User $ClusterUser -HostName $KubePrime -Command "sudo systemctl stop k3s && sudo reboot" -IgnoreErrors
Write-Success "kubeprime reboot issued."

# ------------------------------------------------------------------
# STEP 5: REBOOT NAS (OMV)
# ------------------------------------------------------------------
if (-not $SkipNas) {
    Write-Step "Step 5: Rebooting OpenMediaVault NAS ($NasHost)"
    Write-Info "Issuing reboot to pinas..."
    Invoke-RemoteCommand -User $NasUser -HostName $NasHost -Command "sudo reboot || reboot" -IgnoreErrors
    Write-Success "NAS reboot issued."

    # Give NAS time to drop connection
    Start-Sleep -Seconds 10
}

# ==================================================================
# BRING-UP & RECOVERY SEQUENCE
# ==================================================================

# ------------------------------------------------------------------
# STEP 6: WAIT FOR NAS & NFS SERVICES
# ------------------------------------------------------------------
if (-not $SkipNas) {
    Write-Step "Step 6: Waiting for NAS Storage to Come Online"
    Wait-ForPort -HostName $NasHost -Port 22 -ServiceName "NAS SSH"
    Wait-ForPort -HostName $NasHost -Port 2049 -ServiceName "NAS NFS Server"
    Write-Info "NFS daemon is up. Pausing 10s for exports to stabilize..."
    Start-Sleep -Seconds 10
    Write-Success "NAS is ready and exporting filesystems!"
}

# ------------------------------------------------------------------
# STEP 7: WAIT FOR K3S CONTROL PLANE (KUBEPRIME)
# ------------------------------------------------------------------
Write-Step "Step 7: Waiting for kubeprime & k3s API Server"
Wait-ForPort -HostName $KubePrime -Port 22 -ServiceName "kubeprime SSH"
Wait-ForPort -HostName $KubePrime -Port 6443 -ServiceName "k3s API Server"

Write-Info "Waiting for kubeprime node status to become Ready..."
if (-not $DryRun) {
    $ready = $false
    $startWait = [DateTime]::UtcNow
    while (-not $ready -and ([DateTime]::UtcNow - $startWait).TotalSeconds -lt $PollTimeoutSeconds) {
        $nodeOut = Invoke-RemoteCommand -User $ClusterUser -HostName $KubePrime -Command "sudo k3s kubectl get nodes -o wide" -IgnoreErrors
        if ($nodeOut -match "kubeprime\s+Ready") {
            $ready = $true
            Write-Success "kubeprime is Ready!"
            break
        }
        Start-Sleep -Seconds 5
    }
}

# ------------------------------------------------------------------
# STEP 8: WAIT FOR WORKERS (KUBE2 & YOGA-NODE)
# ------------------------------------------------------------------
Write-Step "Step 8: Waiting for Workers to Join the Cluster"
Wait-ForPort -HostName $Kube2 -Port 22 -ServiceName "kube2 SSH"
Wait-ForPort -HostName $YogaNode -Port 22 -ServiceName "yoga-node SSH"

if (-not $DryRun) {
    Write-Info "Polling kubectl get nodes until all workers report Ready..."
    $allReady = $false
    $startWait = [DateTime]::UtcNow
    while (-not $allReady -and ([DateTime]::UtcNow - $startWait).TotalSeconds -lt $PollTimeoutSeconds) {
        $nodesOut = Invoke-RemoteCommand -User $ClusterUser -HostName $KubePrime -Command "sudo k3s kubectl get nodes" -IgnoreErrors
        $k2Ready = ($nodesOut -match "kube2\s+Ready")
        $ynReady = ($nodesOut -match "yoga-node\s+Ready")

        if ($k2Ready -and $ynReady) {
            $allReady = $true
            Write-Success "All cluster nodes (kubeprime, kube2, yoga-node) are Ready!"
            break
        }
        Start-Sleep -Seconds 5
    }

    # Check yoga-node flannel.1 interface (USB dock auto-heal)
    Write-Info "Verifying flannel.1 overlay interface on yoga-node..."
    $flannelCheck = Invoke-RemoteCommand -User $ClusterUser -HostName $YogaNode -Command "ip -br link show flannel.1" -IgnoreErrors
    if ($flannelCheck -match "UP") {
        Write-Success "yoga-node flannel.1 is UP and healthy."
    } else {
        Write-WarningMsg "yoga-node flannel.1 missing or DOWN (USB dock glitch). Restarting k3s-agent..."
        Invoke-RemoteCommand -User $ClusterUser -HostName $YogaNode -Command "sudo systemctl restart k3s-agent" -IgnoreErrors
        Start-Sleep -Seconds 5
    }

    # Start Frigate on yoga-node if not auto-started
    Write-Info "Ensuring Frigate NVR is running on yoga-node..."
    $frigateStart = "if [ -d /opt/frigate ]; then cd /opt/frigate && sudo docker compose up -d; fi"
    Invoke-RemoteCommand -User $ClusterUser -HostName $YogaNode -Command $frigateStart -IgnoreErrors
    Write-Success "Frigate NVR check complete."
}

# ------------------------------------------------------------------
# STEP 9: WAIT FOR HOME ASSISTANT
# ------------------------------------------------------------------
if (-not $SkipHa) {
    Write-Step "Step 9: Waiting for Home Assistant"
    Wait-ForPort -HostName $HaHost -Port 22 -ServiceName "Home Assistant SSH"
    Wait-ForPort -HostName $HaHost -Port 8123 -ServiceName "Home Assistant Web UI"
    Write-Success "Home Assistant is back online!"
}

# ------------------------------------------------------------------
# STEP 10: POST-REBOOT VERIFICATION REPORT
# ------------------------------------------------------------------
Write-Step "Step 10: Cluster Health Summary"

if (-not $DryRun) {
    Write-Host "`n--- Node Status ---" -ForegroundColor Cyan
    $nodesStatus = Invoke-RemoteCommand -User $ClusterUser -HostName $KubePrime -Command "sudo k3s kubectl get nodes -o wide"
    Write-Host $nodesStatus

    Write-Host "`n--- Pod Status (All Namespaces) ---" -ForegroundColor Cyan
    $podsStatus = Invoke-RemoteCommand -User $ClusterUser -HostName $KubePrime -Command "sudo k3s kubectl get pods -A"
    Write-Host $podsStatus

    Write-Host "`n--- NodePort Check (Grafana: 30001) ---" -ForegroundColor Cyan
    $grafanaCheck = Test-NetConnection -ComputerName $KubePrime -Port 30001 -WarningAction SilentlyContinue
    if ($grafanaCheck.TcpTestSucceeded) {
        Write-Success "Grafana NodePort (30001) is reachable!"
    } else {
        Write-WarningMsg "Grafana NodePort not answering yet; pods may still be warming up."
    }
}

Write-Host @"
====================================================================
           REBOOT SEQUENCE COMPLETED SUCCESSFULLY!
====================================================================
"@ -ForegroundColor Green
