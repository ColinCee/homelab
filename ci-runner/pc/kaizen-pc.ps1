# How many Auto Kaizen build slots Colin's PC gives: kaizen-pc 6, kaizen-pc off, kaizen-pc on
# (back to the last number). keeper.ps1 applies it within 5 s; off lets running jobs finish,
# then powers the guest off. Gaming needs nothing: a busy GPU gives 0 slots on its own.
param([Parameter(Mandatory)][string]$Slots)
$root = 'D:\kaizen-ci'
$file = "$root\capacity.txt"
$last = "$root\capacity.last"
switch -Regex ($Slots) {
    '^\d+$' { Set-Content $file $Slots; Set-Content $last $Slots }
    '^off$' { Set-Content $file 'off' }
    '^on$' { Set-Content $file ((Get-Content $last -ErrorAction SilentlyContinue) ?? '2') }
    default { Write-Error 'kaizen-pc N | off | on'; exit 2 }
}
"PC build slots: $(Get-Content $file) (keeper log: $root\keeper.log)"
