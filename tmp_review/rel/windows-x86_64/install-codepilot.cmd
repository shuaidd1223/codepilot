@echo off
setlocal
set SCRIPT_DIR=%~dp0
set TARGET_DIR=%LOCALAPPDATA%\Programs\CodePilot\bin
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"
copy /Y "%SCRIPT_DIR%foo.exe" "%TARGET_DIR%\foo.exe" >nul
powershell -NoProfile -Command "$dir=$env:LOCALAPPDATA + '\Programs\CodePilot\bin';$current=[Environment]::GetEnvironmentVariable('Path','User');if(-not $current){$current=''};$parts=@($current -split ';' | Where-Object { $_ -ne '' });if($parts -notcontains $dir){$updated=if($current){$current + ';' + $dir}else{$dir};[Environment]::SetEnvironmentVariable('Path',$updated,'User')};"
echo CodePilot 已安装到 %TARGET_DIR%
echo 请重新打开终端后再运行 codepilot --help
endlocal
