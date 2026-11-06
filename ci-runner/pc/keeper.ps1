# Runs at Colin's sign-in (Startup folder): gives the kaizen-ci guest the slots set with
# kaizen-pc.ps1, none while the GPU is busy (a game or a GPU job), and holds its RAM lease.
# Works in PowerShell's ConstrainedLanguage mode: cmdlets and plain programs only.
$ErrorActionPreference = 'Continue'
$root = 'D:\kaizen-ci'
$vm = 'kaizen-ci'
# A hung ssh would stop the loop and its lease renewals: no prompts, no input, and given up after 15 s of silence.
$guest = @('-n', '-i', "$root\ssh\id_ed25519", '-o', "UserKnownHostsFile=$root\ssh\known_hosts", '-o', 'ConnectTimeout=10',
    '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3', 'kaizen@192.168.250.10')
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

# What the lease band above Claude's prompt shows after kaizen-ci: the PC's runners busy/online, from
# GitHub (the PC registers all its runners on auto-kaizen), or ‖ while paused for the GPU. Given up
# after 20 s, so a hung call cannot stop the lease renewals.
function Status($paused) {
    if ($paused) { return '‖' }
    $out = "$rootunners.json"
    Remove-Item $out -ErrorAction SilentlyContinue
    $call = Start-Process gh -ArgumentList "api repos/$repo/actions/runners?per_page=100" -RedirectStandardOutput $out -WindowStyle Hidden -PassThru
    Wait-Process -InputObject $call -Timeout 20 -ErrorAction SilentlyContinue
    if (-not $call.HasExited) { Stop-Process -InputObject $call -Force; return '?' }
    try { $pc = (Get-Content $out -Raw | ConvertFrom-Json).runners | Where-Object { $_.name -like 'pc-*' -and $_.status -eq 'online' } }
    catch { return '?' }
    return "$(@($pc | Where-Object { $_.busy }).Count)/$(@($pc).Count)"
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

$capSet = [int](& gh variable get KAIZEN_TASK_SLOTS -R $repo 2>$null | Select-Object -First 1)
if ($capSet -le 0) { $capSet = $beelinkSlots }
$given = -1; $slotsSaid = [datetime]::MinValue; $capSince = Get-Date; $capTried = [datetime]::MinValue; $busyRuns = 0; $lastBusy = [datetime]::MinValue; $capped = $false
$leaseGb = 0
$status = '?'; $statusAsked = [datetime]::MinValue
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
        if ($gaming -ne ($status -eq '‖') -or ((Get-Date) - $statusAsked).TotalSeconds -ge 60) {
            $status = Status $gaming; $statusAsked = Get-Date
        }
        $gb = 12
        try { $gb = [int]((Get-VM -Name $vm -ErrorAction Stop).MemoryStartup / 1GB) } catch { }
        if ($leaseGb -eq 0) {
            & lease take --name $vm --res "ram:$gb" --pid $PID --note 'kaizen-ci Hyper-V VM (Auto Kaizen builds)' --status $status | Out-Null
            if ($LASTEXITCODE -eq 0) { $leaseGb = $gb }
        } else {
            & lease renew --name $vm --pid $PID --status $status | Out-Null
            if ($LASTEXITCODE -eq 2) {
                # An error, such as a lease command too old for --status: take it again next time.
                Say 'lease renew failed: taking it again'
                $leaseGb = 0
            } elseif ($LASTEXITCODE -eq 3) {
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
    # Said again every minute too: a guest that booted since reads an older count as 0.
    $stale = ((Get-Date) - $slotsSaid).TotalSeconds -ge 60
    if ($running -and ($slots -ne $given -or $stale) -and (SetGuestSlots $slots)) {
        if ($slots -ne $given) { Say "slots: $slots" }
        $given = $slots; $slotsSaid = Get-Date
    }

    # Auto Kaizen starts at most this many tasks at once (auto-kaizen's start.py): the Beelink's
    # build slots plus the PC's VMs actually up (ready or busy), so a closed door raises nothing.
    $up = if ($running) { GuestVms } else { 0 }
    if ($up -gt $given) { $up = $given }
    if ($up -lt 0) { $up = 0 }
    $cap = $beelinkSlots + $up
    if ($cap -ne $capSet) {
        $due = $cap -lt $capSet -or ((Get-Date) - $capSince).TotalSeconds -ge $riseSeconds
        if ($due -and ((Get-Date) - $capTried).TotalSeconds -ge 60) {
            $capTried = Get-Date
            & gh variable set KAIZEN_TASK_SLOTS --body "$cap" -R $repo
            if ($LASTEXITCODE -eq 0) { $capSet = $cap; Say "task cap: $cap" } else { Say "task cap not set ($cap)" }
        }
    } else { $capSince = Get-Date }
    Start-Sleep 5
}
