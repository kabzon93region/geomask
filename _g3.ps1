Set-Location Y:\openwrt\geomask
Remove-Item Y:\openwrt\geomask\_g.ps1, Y:\openwrt\geomask\_g2.ps1 -Force -ErrorAction SilentlyContinue
git add -A
git status --short | Out-String -Width 200
git commit -m "v1.5.2: real subscription switch via mihomo /configs reload + node-count verify" | Out-String
git push | Out-String
