# deploy_panel.ps1 - push current geo panel assets to the router (panel files only, no service restart)
$ErrorActionPreference = 'Stop'
$Key = "$env:USERPROFILE\.ssh\openwrt_final"
$HostName = "root@192.168.0.1"
$A = "Y:\openwrt\geomask\assets"

Write-Host '[1/3] upload backend + rpcd + acl + page'
scp -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$A\geo-backend.sh" "$($HostName):/tmp/gb.sh"
scp -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$A\geo-sync" "$($HostName):/tmp/gs.sh"
scp -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$A\acl-geo-sync.json" "$($HostName):/tmp/ac.json"
scp -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$A\geo-sync-page.js" "$($HostName):/tmp/pg.js"

Write-Host '[2/3] install into place + chmod + rpcd reload'
ssh -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 $HostName "cp /tmp/gb.sh /usr/bin/geo-backend.sh; cp /tmp/gs.sh /usr/libexec/rpcd/geo-sync; cp /tmp/ac.json /usr/share/rpcd/acl.d/luci-app-geo-sync.json; cp /tmp/pg.js /www/luci-static/resources/view/geo-sync/main.js; chmod 755 /usr/bin/geo-backend.sh /usr/libexec/rpcd/geo-sync; /etc/init.d/rpcd reload; rm -f /tmp/gb.sh /tmp/gs.sh /tmp/ac.json /tmp/pg.js; echo INSTALLED"

Write-Host '[3/3] verify sizes + methods'
ssh -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 $HostName "wc -c /usr/bin/geo-backend.sh /usr/libexec/rpcd/geo-sync /www/luci-static/resources/view/geo-sync/main.js /usr/share/rpcd/acl.d/luci-app-geo-sync.json; grep -c gs-subs /www/luci-static/resources/view/geo-sync/main.js"
Write-Host 'DEPLOY_PANEL_DONE'
