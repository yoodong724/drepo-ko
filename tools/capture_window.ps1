param(
    [Parameter(Mandatory = $true)][int]$TargetProcessId,
    [Parameter(Mandatory = $true)][string]$Screenshot,
    [int]$DelaySeconds = 10,
    [string]$InputKeys,
    [int]$PostInputDelaySeconds = 3,
    [int]$ClickX = -1,
    [int]$ClickY = -1
)

# Attach to an already running Windows process. Launching the game separately
# avoids Start-Process blocking on executable paths exposed through WSL UNC.
$ErrorActionPreference = "Stop"
Write-Host "capture: loading drawing support"
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public struct RECT {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
}

public static class WindowCaptureNative {
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);
}
'@

Write-Host "capture: attaching to process $TargetProcessId"
$process = Get-Process -Id $TargetProcessId
try {
    Write-Host "capture: waiting $DelaySeconds seconds"
    Start-Sleep -Seconds $DelaySeconds
    $process.Refresh()
    if ($process.HasExited) {
        throw "Process exited before capture"
    }
    $rect = New-Object RECT
    if (-not [WindowCaptureNative]::GetWindowRect($process.MainWindowHandle, [ref]$rect)) {
        throw "GetWindowRect failed"
    }
    if (($ClickX -ge 0 -and $ClickY -lt 0) -or ($ClickX -lt 0 -and $ClickY -ge 0)) {
        throw "ClickX and ClickY must be set together"
    }
    if (($ClickX -ge 0 -and $ClickY -ge 0) -or -not [string]::IsNullOrWhiteSpace($InputKeys)) {
        Write-Host "capture: activating window"
        $shell = New-Object -ComObject WScript.Shell
        if (-not $shell.AppActivate($process.Id)) {
            throw "Could not activate process window"
        }
        Start-Sleep -Milliseconds 250
        if ($ClickX -ge 0 -and $ClickY -ge 0) {
            $windowWidth = $rect.Right - $rect.Left
            $windowHeight = $rect.Bottom - $rect.Top
            if ($ClickX -ge $windowWidth -or $ClickY -ge $windowHeight) {
                throw "Click position is outside the window: $ClickX,$ClickY in ${windowWidth}x${windowHeight}"
            }
            Write-Host "capture: clicking relative position $ClickX,$ClickY"
            if (-not [WindowCaptureNative]::SetCursorPos($rect.Left + $ClickX, $rect.Top + $ClickY)) {
                throw "SetCursorPos failed"
            }
            [WindowCaptureNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
            [WindowCaptureNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
            Start-Sleep -Milliseconds 150
        }
        if (-not [string]::IsNullOrWhiteSpace($InputKeys)) {
            Write-Host "capture: sending keys"
            $shell.SendKeys($InputKeys)
        }
        Start-Sleep -Seconds $PostInputDelaySeconds
        $process.Refresh()
        if ($process.HasExited) {
            throw "Process exited after input"
        }
    }
    if (-not [WindowCaptureNative]::GetWindowRect($process.MainWindowHandle, [ref]$rect)) {
        throw "GetWindowRect failed"
    }
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    if ($width -le 0 -or $height -le 0) {
        throw "Invalid window dimensions: ${width}x${height}"
    }
    Write-Host "capture: copying ${width}x${height} window"
    $bitmap = New-Object System.Drawing.Bitmap($width, $height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
        $bitmap.Save($Screenshot, [System.Drawing.Imaging.ImageFormat]::Png)
        Write-Host "capture: saved $Screenshot"
    }
    finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
    [pscustomobject]@{
        screenshot = $Screenshot
        width = $width
        height = $height
        process_id = $process.Id
    } | ConvertTo-Json -Compress
}
finally {}
