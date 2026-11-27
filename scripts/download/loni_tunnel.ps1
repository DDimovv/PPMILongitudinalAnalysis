param(
    [Parameter(Mandatory = $true)][string]$User,
    [string]$FrontNode = "10.10.65.6",
    [int]$Port = 1080,
    [string]$Chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
)

if (Get-Process chrome -ErrorAction SilentlyContinue) {
    Write-Error "Close all Chrome windows first, then run this again."; exit 1
}
$pac = @"
function FindProxyForURL(url, host) {
  if (dnsDomainIs(host, "loni.usc.edu") || host == "loni.usc.edu" || host == "api.ipify.org") {
    return "SOCKS5 127.0.0.1:$Port";
  }
  return "DIRECT";
}
"@
Start-Process ssh -ArgumentList "-N", "-D", "127.0.0.1:$Port", "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30", "$User@$FrontNode"
Start-Sleep -Seconds 3
$ip = curl.exe -sS --max-time 20 --socks5-hostname "127.0.0.1:$Port" https://api.ipify.org
Write-Host "LONI will see this IP: $ip (must be the cluster's egress IP)"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pac))
Start-Process $Chrome -ArgumentList "--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,$b64",
    "https://ida.loni.usc.edu/"
