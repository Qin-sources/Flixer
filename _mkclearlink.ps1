$W = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Flixer 清空网盘缓存.lnk'
$S = $W.CreateShortcut($lnk)
$S.TargetPath = 'wscript.exe'
$S.Arguments = '"F:\新建文件夹\Flixer\清空网盘缓存.vbs"'
$S.WorkingDirectory = 'F:\新建文件夹\Flixer'
$S.IconLocation = 'F:\新建文件夹\Flixer\flixer.ico,0'
$S.Description = '清空 Flixer 在夸克网盘的缓存，释放空间'
$S.Save()
Write-Output ('created=' + (Test-Path $lnk))
Write-Output ('path=' + $lnk)
