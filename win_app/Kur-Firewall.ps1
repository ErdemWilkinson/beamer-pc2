# Beamer'ın güvenlik duvarı kuralları, kaynaktan çalıştırılan kurulum için (kur.ps1 bunu yönetici
# olarak çağırır). -Exe, portları gerçekten dinleyen Python yorumlayıcısıdır: venv'deki pythonw.exe
# yalnızca onu başlatır. Kurallar sadece Özel (Private) ağlarda açılır, Herkese Açık ağlarda asla.
param([Parameter(Mandatory = $true)][string]$Exe)

$ErrorActionPreference = 'Stop'
try {
    # Beamer Herkese Açık ağlarda bilerek kural açmaz; ev Wi-Fi'ı Özel olmalı. Bir ağı Özel yapmak
    # onu güvenilir saymaktır, bu yüzden her ağ için ayrı ayrı sorulur, hiçbiri kendiliğinden değişmez.
    foreach ($network in @(Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq 'Public' })) {
        $answer = Read-Host "'$($network.Name)' ağı Herkese Açık. Bu sizin ev/güvendiğiniz ağınız mı, Özel yapılsın mı? (E/H)"
        if ($answer -match '^[EeYy]') {
            $network | Set-NetConnectionProfile -NetworkCategory Private
            Write-Host "'$($network.Name)' Özel yapıldı." -ForegroundColor Green
        } else {
            Write-Host "'$($network.Name)' Herkese Açık kaldı; Beamer bu ağda iki PC'yi bağlayamaz." -ForegroundColor Yellow
        }
    }

    $rules = @(
        @{ Name = 'Beamer Receiver (TCP-In)'; Protocol = 'TCP'; Port = 24820 },
        @{ Name = 'Beamer Pairing (UDP-In)'; Protocol = 'UDP'; Port = 24821 },
        @{ Name = 'Beamer Pairing (TCP-In)'; Protocol = 'TCP'; Port = 24821 },
        @{ Name = 'Beamer Transfer (TCP-In)'; Protocol = 'TCP'; Port = 24823 }
    )
    foreach ($rule in $rules) {
        # Aynı program için daha önce yazılmış kural varsa önce o silinir, kurallar çoğalmaz.
        foreach ($old in @(Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue)) {
            $filter = $old | Get-NetFirewallApplicationFilter
            if ([string]::Equals($filter.Program, $Exe, [StringComparison]::OrdinalIgnoreCase)) {
                $old | Remove-NetFirewallRule
            }
        }
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Protocol $rule.Protocol `
            -LocalPort $rule.Port -Action Allow -Profile Private -Program $Exe | Out-Null
    }
    Write-Host "Güvenlik duvarı kuralları yazıldı: $Exe" -ForegroundColor Green
    Get-NetFirewallRule -DisplayName 'Beamer*' | Select-Object DisplayName, Enabled, Profile | Format-Table -AutoSize
} catch {
    Write-Host "Güvenlik duvarı ayarlanamadı: $_" -ForegroundColor Red
    Read-Host 'Kapatmak için Enter'
    exit 1
}
Start-Sleep -Seconds 3
