# Beamer PC2 - one-line setup from source, for a second (or any) Windows PC.
#
#   irm https://raw.githubusercontent.com/ErdemWilkinson/beamer-pc2/main/kur.ps1 | iex
#
# Installs Python 3.11 and Git if missing (winget), clones or updates this repo into
# %USERPROFILE%\beamer-pc2, installs the dependencies, writes the firewall rules (asks for admin
# once), adds Desktop / Start menu / sign-in shortcuts and starts Beamer. Safe to run again: it
# updates instead of reinstalling. ASCII only, so it runs the same through iex or as a file.
#
# Beamer's own Update button runs this file with BEAMER_PC2_UPDATE=1 and BEAMER_PC2_DIR set to
# the install it runs from: shortcuts are left alone and admin is asked for only when a firewall
# rule is missing. Any other folder is never reset: it may hold work that is not on GitHub.
& {
    $ErrorActionPreference = 'Stop'
    $RepoUrl = 'https://github.com/ErdemWilkinson/beamer-pc2.git'
    $Home2 = Join-Path $env:USERPROFILE 'beamer-pc2'
    $Dir = if ($env:BEAMER_PC2_DIR) { $env:BEAMER_PC2_DIR } else { $Home2 }
    $Updating = $env:BEAMER_PC2_UPDATE -eq '1'
    Remove-Item Env:BEAMER_PC2_UPDATE, Env:BEAMER_PC2_DIR -ErrorAction SilentlyContinue
    $Rules = @('Beamer Receiver (TCP-In)', 'Beamer Pairing (UDP-In)', 'Beamer Pairing (TCP-In)', 'Beamer Transfer (TCP-In)')

    function Say([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }

    function Refresh-Path {
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
    }

    function Have-Python {
        try { & py -3.11 -c "print(1)" *> $null; return $LASTEXITCODE -eq 0 } catch { return $false }
    }

    function Install-Package([string]$id) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "winget bulunamadi. Microsoft Store'dan 'App Installer' kurup tekrar deneyin ($id icin)."
        }
        Say "$id kuruluyor (winget)..."
        winget install -e --id $id --accept-source-agreements --accept-package-agreements --silent
        Refresh-Path
    }

    try {
        Say 'Beamer PC2 kurulumu basliyor'

        if (-not (Have-Python)) { Install-Package 'Python.Python.3.11' }
        if (-not (Have-Python)) { throw 'Python 3.11 kurulamadi. PowerShell penceresini kapatip acin ve tekrar calistirin.' }
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Install-Package 'Git.Git' }
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git kurulamadi. PowerShell penceresini kapatip acin ve tekrar calistirin.' }

        if ((Test-Path (Join-Path $Dir '.git')) -and
                ([IO.Path]::GetFullPath($Dir).TrimEnd('\') -ne [IO.Path]::GetFullPath($Home2).TrimEnd('\'))) {
            throw "$Dir kurulum klasoru degil ($Home2); icindeki calismayi silmemek icin guncellenmedi."
        }

        # pc2-hizlandir.ps1 runs Beamer with administrator rights from scheduled tasks; then Beamer
        # is stopped and started through them, which works without being administrator here.
        $Elevated = [bool](Get-ScheduledTask -TaskName 'Beamer PC2' -ErrorAction SilentlyContinue)
        if ($Elevated) {
            foreach ($task in @('Beamer PC2', 'Beamer PC2 (Ac)')) {
                try { Stop-ScheduledTask -TaskName $task -ErrorAction Stop } catch { Write-Host "  ($task durdurulamadi: $_)" }
            }
        }

        # Stop a Beamer started from this folder, so its files can be updated.
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
            Where-Object { $_.CommandLine -like "*$Dir*kvm_bridge_win.py*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

        if (Test-Path (Join-Path $Dir '.git')) {
            # This folder is an install, not a place to edit: it always matches GitHub, even when
            # the history there was rewritten (a plain pull would refuse to fast-forward).
            Say "Guncelleniyor: $Dir"
            git -C $Dir fetch origin
            if ($LASTEXITCODE -eq 0) { git -C $Dir reset --hard origin/main }
        } else {
            Say "Indiriliyor: $Dir"
            git clone $RepoUrl $Dir
        }
        if ($LASTEXITCODE -ne 0) { throw 'git basarisiz oldu.' }

        $Venv = Join-Path $Dir '.venv'
        if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
            Say 'Python ortami olusturuluyor'
            & py -3.11 -m venv $Venv
        }
        Say 'Bagimliliklar kuruluyor (birkac dakika surebilir)'
        & (Join-Path $Venv 'Scripts\python.exe') -m pip install --disable-pip-version-check -q -r (Join-Path $Dir 'win_app\requirements-win.txt')
        if ($LASTEXITCODE -ne 0) { throw 'pip install basarisiz oldu.' }

        # The venv's pythonw.exe only starts the base interpreter, which is what listens on the ports.
        $BaseExe = (& (Join-Path $Venv 'Scripts\python.exe') -c "import os,sys;print(os.path.join(sys.base_exec_prefix,'pythonw.exe'))").Trim()
        $missing = @($Rules | Where-Object { -not (Get-NetFirewallRule -DisplayName $_ -ErrorAction SilentlyContinue) })
        if (-not $Updating -or $missing.Count -gt 0) {
            Say "Guvenlik duvari ayarlaniyor (yonetici izni istenecek): $BaseExe"
            try {
                $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Dir 'win_app\Kur-Firewall.ps1')`" -Exe `"$BaseExe`""
                Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList $arguments
            } catch {
                Write-Host 'Yonetici izni verilmedi; guvenlik duvari kurallari yazilmadi. Bu komutu tekrar calistirip izin verebilirsiniz.' -ForegroundColor Yellow
            }
        }

        $Pythonw = Join-Path $Venv 'Scripts\pythonw.exe'
        $Script = Join-Path $Dir 'win_app\kvm_bridge_win.py'
        if ($Updating) {
            Say 'Beamer yeniden baslatiliyor'
            if ($Elevated) { Start-ScheduledTask -TaskName 'Beamer PC2 (Ac)' }
            else { Start-Process $Pythonw -ArgumentList "`"$Script`"" -WorkingDirectory $Dir }
            Write-Host 'Guncelleme tamam. Bu pencere kendiliginden kapanacak.' -ForegroundColor Green
            Start-Sleep -Seconds 4
            return
        }
        if ($Elevated) {
            # The shortcuts already run the tasks; rewriting them would start a normal Beamer.
            Say 'Beamer baslatiliyor (yonetici yetkisiyle, gorev uzerinden)'
            Start-ScheduledTask -TaskName 'Beamer PC2 (Ac)'
            Write-Host ''
            Write-Host 'Guncelleme tamam.' -ForegroundColor Green
            return
        }

        Say 'Kisayollar olusturuluyor (Masaustu, Baslat menusu, oturum acilisi)'
        $Shell = New-Object -ComObject WScript.Shell
        $places = @(
            @{ Folder = [Environment]::GetFolderPath('Desktop'); Args = "`"$Script`"" },
            @{ Folder = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'; Args = "`"$Script`"" },
            @{ Folder = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'; Args = "`"$Script`" --hidden" }
        )
        foreach ($place in $places) {
            $link = $Shell.CreateShortcut((Join-Path $place.Folder 'Beamer PC2.lnk'))
            $link.TargetPath = $Pythonw
            $link.Arguments = $place.Args
            $link.WorkingDirectory = $Dir
            $link.IconLocation = Join-Path $Dir 'win_app\Beamer.ico'
            $link.Description = 'Beamer PC2'
            $link.Save()
        }

        Say 'Beamer baslatiliyor (saatin yanindaki tepside)'
        Start-Process $Pythonw -ArgumentList "`"$Script`"" -WorkingDirectory $Dir

        Write-Host ''
        Write-Host 'Kurulum tamam.' -ForegroundColor Green
        Write-Host 'Simdi: iki PC''yi 6 haneli kodla eslestirin; bu PC''de tepsiden "Machines drive this PC" acik olsun;'
        Write-Host 'Crossing sayfasinda PC''lerin yerlesimini ayarlayin. Ayrintilar: docs\PC2-AKTARIM.md'
    } catch {
        Write-Host "Kurulum durdu: $_" -ForegroundColor Red
        if ($Updating) { Read-Host 'Kapatmak icin Enter' | Out-Null }
    }
}
