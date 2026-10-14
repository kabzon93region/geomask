# push_and_run.ps1 - upload a local sh script to the router and run it
param(
    [string]$Script = "Y:\openwrt\geomask\sandbox\_migrate.sh",
    [string]$Remote = "/tmp/mg.sh"
)
$Key = "$env:USERPROFILE\.ssh\openwrt_final"
$Router = "root@192.168.0.1"
Write-Host "[1/2] scp $Script -> $Remote"
scp -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 $Script "${Router}:$Remote"
Write-Host "[2/2] run $Remote"
ssh -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 $Router "sh $Remote"
