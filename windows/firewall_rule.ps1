param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Status', 'HasRules', 'Authorize', 'Remove')]
    [string]$Action,
    [string]$ReceiverPath,
    [string]$InterfaceAlias,
    [string]$ResultPath
)

[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$ErrorActionPreference = 'Stop'
$stage = 'NETWORK_INFO_FAILED'
function Complete-Result($success, $status, $message, $created = $false, $verified = $false) {
    $json = [pscustomobject]@{success=$success; status=$status; errorCode=$(if($success){$null}else{$stage}); message=$message; ruleCreated=$created; ruleVerified=$verified; exceptionType=$(if($success){$null}else{$script:exceptionType})} | ConvertTo-Json -Compress
    if ($ResultPath) {
        $target = [IO.Path]::GetFullPath($ResultPath)
        $temporary = $target + '.writing'
        [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temporary, $target)
    }
    Write-Output $json
}
$ruleGroup = 'SendToCopyQ {8D16AF61-0A5F-48C1-B6D0-4CF3A9876E30}'
$rulePrefix = 'SendToCopyQ-LAN-Receiver-'

function Get-CurrentProfile([string]$Alias) {
    $connection = Get-NetConnectionProfile -InterfaceAlias $Alias -ErrorAction Stop | Select-Object -First 1
    if (-not $connection) { throw "无法读取网卡 ${Alias} 的 Windows 网络配置文件。" }
    switch ([string]$connection.NetworkCategory) {
        'DomainAuthenticated' { return 'Domain' }
        'Private' { return 'Private' }
        'Public' { return 'Public' }
        default { throw "网卡 ${Alias} 的网络配置文件无法识别。" }
    }
}

function Get-ProductRules {
    @(Get-NetFirewallRule -PolicyStore ActiveStore -Group $ruleGroup -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "$rulePrefix*" })
}

function Get-InstalledReceiverRules([string]$Path) {
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) {
        throw '清理防火墙规则需要当前安装的接收程序完整路径。'
    }
    $expectedPath = [IO.Path]::GetFullPath($Path)
    Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop |
        Where-Object { $_.Group -eq $ruleGroup -and $_.Name -match "^$rulePrefix[0-9A-F]{12}$" } |
        Where-Object {
            $application = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $_ -ErrorAction Stop
            [string]::Equals([string]$application.Program, $expectedPath, [StringComparison]::OrdinalIgnoreCase)
        }
}

function Test-Rule([object]$Rule, [string]$Path, [string]$Alias, [string]$Profile) {
    $application = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $Rule
    $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $Rule
    $address = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $Rule
    $interface = Get-NetFirewallInterfaceFilter -AssociatedNetFirewallRule $Rule
    $programMatches = [string]::Equals([IO.Path]::GetFullPath([string]$application.Program),
        [IO.Path]::GetFullPath($Path), [StringComparison]::OrdinalIgnoreCase)
    $portMatches = ([string]$port.Protocol -eq 'TCP' -and @($port.LocalPort | ForEach-Object { [string]$_ }) -contains '47831' -and @($port.LocalPort).Count -eq 1)
    $remoteMatches = @($address.RemoteAddress | ForEach-Object { [string]$_ }) -contains 'LocalSubnet' -and @($address.RemoteAddress).Count -eq 1
    $interfaceMatches = @($interface.InterfaceAlias | ForEach-Object { [string]$_ }) -contains $Alias -and @($interface.InterfaceAlias).Count -eq 1
    $profileMatches = @($Rule.Profile.ToString().Split(',') | ForEach-Object { $_.Trim() }) -contains $Profile -and @($Rule.Profile.ToString().Split(',')).Count -eq 1
    return ($Rule.Enabled -eq 'True' -and $Rule.Direction -eq 'Inbound' -and $Rule.Action -eq 'Allow' -and
        $programMatches -and $portMatches -and $remoteMatches -and $interfaceMatches -and $profileMatches)
}

try {
    switch ($Action) {
        'Status' {
            $authorized = $false
            $reason = '无法确认接收网卡。'
            $profile = $null
            if ($ReceiverPath -and $InterfaceAlias) {
                try {
                    $profile = Get-CurrentProfile $InterfaceAlias
                    foreach ($rule in (Get-ProductRules)) {
                        if (Test-Rule $rule $ReceiverPath $InterfaceAlias $profile) { $authorized = $true; break }
                    }
                    $reason = if ($authorized) { '已授权' } else { '尚未授权当前接收程序和局域网网卡。' }
                } catch { $reason = $_.Exception.Message }
            }
            [pscustomobject]@{ authorized = $authorized; interface = $InterfaceAlias; profile = $profile; reason = $reason } |
                ConvertTo-Json -Compress | Write-Output
            exit 0
        }
        'HasRules' {
            $stage = 'RULE_LOOKUP_FAILED'
            if (@(Get-InstalledReceiverRules $ReceiverPath).Count -gt 0) { exit 0 } else { exit 1 }
        }
        'Authorize' {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
            $principal = [Security.Principal.WindowsPrincipal]::new($identity)
            if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
                $stage = 'UAC_PERMISSION_DENIED'
                throw '防火墙授权助手未获得管理员权限。'
            }
            if (-not (Test-Path -LiteralPath $ReceiverPath -PathType Leaf)) { throw '找不到已安装的接收核心程序。' }
            $adapter = Get-NetAdapter -Name $InterfaceAlias -ErrorAction Stop | Select-Object -First 1
            if (-not $adapter -or $adapter.Status -ne 'Up' -or
                ($adapter.Name + ' ' + $adapter.InterfaceDescription) -match '(?i)meta|tun|vpn|wintun|loopback|virtual|bluetooth|teredo') {
                throw '当前接收网卡不是可用的物理局域网网卡。'
            }
            $profile = Get-CurrentProfile $InterfaceAlias
            $interfaceAddresses = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction Stop |
                Where-Object { $_.IPAddress -notlike '127.*' } | ForEach-Object { [string]$_.IPAddress })
            $interfaceAddresses = @($interfaceAddresses | Where-Object { $_ -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' })
            if (-not $interfaceAddresses.Count) { throw '当前网卡没有有效的局域网 IPv4 地址。' }
            foreach ($rule in (Get-ProductRules)) {
                if (Test-Rule $rule $ReceiverPath $InterfaceAlias $profile) {
                    Complete-Result $true 'ALREADY_AUTHORIZED' '已存在正确规则，无需重复创建。' $false $true
                    exit 0
                }
            }
            $stage = 'RULE_CREATE_FAILED'
            $identityText = ([IO.Path]::GetFullPath($ReceiverPath) + '|' + $InterfaceAlias + '|' + $profile).ToLowerInvariant()
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $suffix = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($identityText))).Replace('-', '').Substring(0, 12) }
            finally { $sha.Dispose() }
            $ruleName = $rulePrefix + $suffix
            $sameRule = Get-NetFirewallRule -PolicyStore ActiveStore -Name $ruleName -ErrorAction SilentlyContinue
            if ($sameRule) { throw '已有同名规则参数不匹配；未覆盖或删除，请人工检查。' }
            New-NetFirewallRule -Name $ruleName -DisplayName 'SendToCopyQ 局域网图片接收' -Group $ruleGroup `
                -Direction Inbound -Action Allow -Enabled True -Program ([IO.Path]::GetFullPath($ReceiverPath)) `
                -Protocol TCP -LocalPort 47831 -RemoteAddress LocalSubnet -LocalAddress Any `
                -InterfaceAlias $InterfaceAlias -Profile $profile -EdgeTraversalPolicy Block | Out-Null
            $stage = 'RULE_VERIFY_FAILED'
            $createdRule = Get-NetFirewallRule -PolicyStore ActiveStore -Name $ruleName -ErrorAction Stop
            if (-not (Test-Rule $createdRule $ReceiverPath $InterfaceAlias $profile)) { throw '规则已创建，但参数回读验证失败。' }
            Complete-Result $true 'RULE_CREATED' '局域网接收规则已创建并验证。' $true $true
            exit 0
        }
        'Remove' {
            $stage = 'UAC_PERMISSION_DENIED'
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
            $principal = [Security.Principal.WindowsPrincipal]::new($identity)
            if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
                throw '防火墙清理助手未获得管理员权限。'
            }
            $stage = 'RULE_REMOVE_FAILED'
            foreach ($rule in (Get-InstalledReceiverRules $ReceiverPath)) {
                Remove-NetFirewallRule -Name $rule.Name -PolicyStore PersistentStore -Confirm:$false -ErrorAction Stop
            }
            $stage = 'RULE_REMOVE_VERIFY_FAILED'
            if (@(Get-InstalledReceiverRules $ReceiverPath).Count -ne 0) {
                throw '清理后仍检测到当前安装对应的防火墙规则。'
            }
            exit 0
        }
    }
} catch {
    $script:exceptionType = $_.Exception.GetType().FullName
    Complete-Result $false $stage $_.Exception.Message
    exit 2
}
