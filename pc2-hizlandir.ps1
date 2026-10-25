# Beamer PC2 - make this PC a smooth machine to be driven, after kur.ps1 has installed Beamer.
#
#   irm https://raw.githubusercontent.com/ErdemWilkinson/beamer-pc2/main/pc2-hizlandir.ps1 | iex
#
# 1. Wi-Fi power saving off in the current power plan (on battery too). A dozing adapter delivers
#    the mouse in bursts every few tens of milliseconds, which reads as a pointer at 20-30 fps.
# 2. Only with $env:BEAMER_YONETICI = '1' set first: Beamer started at sign-in with administrator
#    rights, by two scheduled tasks: "Beamer PC2" (sign-in, tray only) and "Beamer PC2 (Ac)" (the
#    Desktop and Start menu shortcuts, opens the window). Windows lets a normal program's input reach
#    only normal windows, so without this the pointer stops over Task Manager and other administrator
#    windows. Opt-in, because it weakens UAC: the code and the Python the tasks run live in folders
#    this user can write, so anything running as this user could change them and be started with
#    administrator rights without a prompt. A paired machine can also drive administrator windows.
#    $env:BEAMER_YONETICI = '0' removes the tasks again.
#
# Asks for administrator rights once. ASCII only, so it runs the same through iex or as a file.
& {
    $ErrorActionPreference = 'Stop'
    $Url = 'https://raw.githubusercontent.com/ErdemWilkinson/beamer-pc2/main/pc2-hizlandir.ps1'
    $Choice = [string]$env:BEAMER_YONETICI
    function Say([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }

    # The Wi-Fi setting needs no administrator rights, so it is done first and on its own: an
    # account that cannot elevate (or a refused prompt) still gets it.
    Say 'Wi-Fi guc tasarrufu kapatiliyor (prizde ve pilde)'
    $wifi = '19cbb8fa-5279-450e-9fac-8a3d5fedd0c1'
    $saving = '12bbebe6-58d6-4636-95bb-3217ef867c1a'
    powercfg /setacvalueindex SCHEME_CURRENT $wifi $saving 0
    powercfg /setdcvalueindex SCHEME_CURRENT $wifi $saving 0
    powercfg /setactive SCHEME_CURRENT
    if ($LASTEXITCODE -ne 0) { Write-Host 'Wi-Fi ayari yazilamadi (powercfg basarisiz).' -ForegroundColor Red }

    if ($Choice -notin @('0', '1')) {
        Write-Host ''
        Write-Host 'Tamam: Wi-Fi artik uyuklamiyor.' -ForegroundColor Green
        Write-Host 'Gorev Yoneticisi gibi yonetici pencerelerinde de mouse icin (UAC korumasini zayiflatir, bkz. docs\PC2-AKTARIM.md):'
        Write-Host "  `$env:BEAMER_YONETICI='1'; irm $Url | iex"
        return
    }

    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Say 'Yonetici izni isteniyor...'
        try {
            # An elevated process does not inherit this window's variables: the choice goes along explicitly.
            Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"`$env:BEAMER_YONETICI='$Choice'; irm $Url | iex`""
        } catch {
            Write-Host 'Yonetici izni alinamadi. Bu hesap yonetici degil ya da Windows/antivirus izin vermedi.' -ForegroundColor Yellow
            Write-Host 'Wi-Fi ayari yine de yapildi. Yonetici kisminin bir yonetici hesabinda calismasi gerekir.' -ForegroundColor Yellow
        }
        return
    }

    try {
        $Dir = Join-Path $env:USERPROFILE 'beamer-pc2'
        $Pythonw = Join-Path $Dir '.venv\Scripts\pythonw.exe'
        $Script = Join-Path $Dir 'win_app\kvm_bridge_win.py'
        if (-not (Test-Path $Pythonw)) { throw "Once kurulum komutunu (kur.ps1) calistirin: $Pythonw yok." }

        if ($Choice -eq '0') {
            Say 'Yonetici gorevleri kaldiriliyor'
            foreach ($task in @('Beamer PC2', 'Beamer PC2 (Ac)')) {
                Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
            }
            Write-Host 'Tamam. Normal kisayollar icin kurulum komutunu (kur.ps1) bir kez calistirin.' -ForegroundColor Green
            return
        }

        Say 'Beamer yonetici yetkisiyle baslayacak sekilde ayarlaniyor'
        $user = "$env:USERDOMAIN\$env:USERNAME"
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        $runAs = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName 'Beamer PC2' -Force -Settings $settings -Principal $runAs `
            -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) `
            -Action (New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Script`" --hidden" -WorkingDirectory $Dir) | Out-Null
        Register-ScheduledTask -TaskName 'Beamer PC2 (Ac)' -Force -Settings $settings -Principal $runAs `
            -Action (New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Script`"" -WorkingDirectory $Dir) | Out-Null

        # The sign-in shortcut would start a second, normal Beamer; the others now run the task.
        Remove-Item (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\Beamer PC2.lnk') -ErrorAction SilentlyContinue
        $Shell = New-Object -ComObject WScript.Shell
        foreach ($folder in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
            $link = $Shell.CreateShortcut((Join-Path $folder 'Beamer PC2.lnk'))
            $link.TargetPath = Join-Path $env:WINDIR 'System32\schtasks.exe'
            $link.Arguments = '/run /tn "Beamer PC2 (Ac)"'
            $link.WindowStyle = 7
            $link.WorkingDirectory = $Dir
            $link.IconLocation = Join-Path $Dir 'win_app\Beamer.ico'
            $link.Description = 'Beamer PC2'
            $link.Save()
        }

        Say 'Beamer yeniden baslatiliyor'
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
            Where-Object { $_.CommandLine -like "*$Dir*kvm_bridge_win.py*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
        Start-ScheduledTask -TaskName 'Beamer PC2'

        Write-Host ''
        Write-Host 'Tamam. Wi-Fi artik uyuklamiyor ve Beamer Gorev Yoneticisi gibi pencerelerde de calisiyor.' -ForegroundColor Green
        Write-Host 'Geri almak icin: Gorev Zamanlayici''da "Beamer PC2" gorevlerini silip kurulum komutunu tekrar calistirin.'
    } catch {
        Write-Host "Durdu: $_" -ForegroundColor Red
    }
    Read-Host 'Kapatmak icin Enter' | Out-Null
}
