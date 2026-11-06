# Runs at Colin's sign-in (Startup folder): gives the kaizen-ci guest the slots set with
# kaizen-pc.ps1, none while the GPU is busy (a game or a GPU job), and holds its RAM lease.
# Works in PowerShell's ConstrainedLanguage mode: cmdlets and plain programs only.
$ErrorActionPreference = 'Continue'
$root = 'D:\kaizen-ci'
$vm = 'kaizen-ci'
$guest = @('-i', "$root\ssh\id_ed25519", '-o', "UserKnownHostsFile=$root\ssh\known_hosts", '-o', 'ConnectTimeout=10', 'kaizen@192.168.250.10')
$repo = 'ColinCee/auto-kaizen'
$beelinkSlots = 5
# GPU busy: over this utilisation for 3 samples in a row (15 s). Back to full after 5 quiet minutes.
$busyPercent = 30
$quietSeconds = 300
# The task cap rises only after the slots have held this long, and drops at once.
$riseSeconds = 600

function Say($text) { Add-Content "$root\keeper.log" "$(Get-Date -Format s) $text" }

function Wanted {
    $text = (Get-Content "$root\capacity.txt" -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($text -match '^\d+$') { return [int]$text }
    return 0
}

function GuestUp {
    & ssh @guest true 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function GuestVms {
    $n = & ssh @guest "systemctl list-units --no-legend 'homelab-ci-vm-*' | wc -l" 2>$null
    if ($n -match '^\d+$') { return [int]$n }
    return -1
}

function GpuBusy {
    $use = & nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>$null | Select-Object -First 1
    $held = (& lease show --json 2>$null | ConvertFrom-Json).holds |
        Where-Object { $_.name -ne $vm -and ($_.wants | Where-Object { $_.resource -eq 'gpu' }) }
    return ($held -or (($use -match '^\d+$') -and [int]$use -gt $busyPercent))
}

function SetGuestSlots($n) {
    & ssh @guest "echo $n | sudo tee /etc/homelab-ci/pc-slots >/dev/null"
    return $LASTEXITCODE -eq 0
}

function SetCpuCap($capped) {
    try {
        if ($capped) { Set-VMProcessor -VMName $vm -Maximum 10 -RelativeWeight 10 -ErrorAction Stop }
        else { Set-VMProcessor -VMName $vm -Maximum 100 -RelativeWeight 100 -ErrorAction Stop }
    } catch { Say "CPU cap not set (needs Hyper-V Administrators): $_" }
}

$given = -1; $capSet = 5; $capSince = Get-Date; $busyRuns = 0; $lastBusy = [datetime]::MinValue; $capped = $false
$leaseGb = 0
while ($true) {
    $want = Wanted
    if (GpuBusy) { $busyRuns++ } else { $busyRuns = 0 }
    if ($busyRuns -ge 3) { $lastBusy = Get-Date }
    $gaming = ((Get-Date) - $lastBusy).TotalSeconds -lt $quietSeconds
    if ($gaming -ne $capped) { SetCpuCap $gaming; $capped = $gaming; Say "gaming: $gaming" }

    # The guest's RAM is held whenever it runs; a forced free (lease free --force --name kaizen-ci)
    # powers it off at once, and it stays off until kaizen-pc.ps1 sets the slots again.
    $running = GuestUp
    if ($running -and $want -eq 0 -and (GuestVms) -eq 0) {
        # kaizen-pc.ps1 off, and the last job has finished: give the RAM back.
        Say 'off and idle: powering the guest off'
        & ssh @guest 'sudo poweroff'
        $running = $false
    }
    if (-not $running -and $leaseGb -gt 0) {
        & lease free --name $vm --pid $PID | Out-Null
        $leaseGb = 0
    }
    if ($running) {
        $gb = 12
        try { $gb = [int]((Get-VM -Name $vm -ErrorAction Stop).MemoryStartup / 1GB) } catch { }
        if ($leaseGb -eq 0) {
            & lease take --name $vm --res "ram:$gb" --pid $PID --note 'kaizen-ci Hyper-V VM (Auto Kaizen builds)' | Out-Null
            if ($LASTEXITCODE -eq 0) { $leaseGb = $gb }
        } else {
            & lease renew --name $vm --pid $PID | Out-Null
            if ($LASTEXITCODE -eq 3) {
                Say 'lease freed: powering the guest off'
                & ssh @guest 'sudo poweroff'
                Set-Content "$root\capacity.txt" 'off'
                $leaseGb = 0; $running = $false
            }
        }
    } elseif ($want -gt 0) {
        try { Start-VM -Name $vm -ErrorAction Stop; Say 'guest started' } catch { Say "guest not started: $_" }
    }

    $slots = if ($gaming -or -not $running) { 0 } else { $want }
    if ($running -and $slots -ne $given -and (SetGuestSlots $slots)) { $given = $slots; Say "slots: $slots" }

    # Auto Kaizen starts at most this many tasks at once (auto-kaizen's start.py).
    $cap = $beelinkSlots + $(if ($running) { $given } else { 0 })
    if ($cap -ne $capSet) {
        if ($cap -lt $capSet -or ((Get-Date) - $capSince).TotalSeconds -ge $riseSeconds) {
            & gh variable set KAIZEN_TASK_SLOTS --body "$cap" -R $repo
            if ($LASTEXITCODE -eq 0) { $capSet = $cap; Say "task cap: $cap" }
        }
    } else { $capSince = Get-Date }
    Start-Sleep 5
}
